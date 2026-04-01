import pandas as pd
import pandas_ta as ta
import yfinance as yf
import logging
import numpy as np

logger = logging.getLogger(__name__)

class MeanReversionStrategy:
    """
    Implements the core strategic logic for the trading bot based on Bollinger Bands,
    RSI, and an ATR Black Swan filter. Uses yfinance for reliable market data.
    """
    def __init__(self, bb_length=20, bb_std=2.0, rsi_length=14, rsi_threshold=30, smart_regime_enabled=False, tp_target_mode="Mean"):
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold
        self.smart_regime_enabled = smart_regime_enabled
        self.tp_target_mode = tp_target_mode
        self.volume_min_pct = 0.8  # Default volume threshold

    def get_historical_data(self, ticker: str, interval="15m", period="10d") -> pd.DataFrame:
        """
        Fetches historical OHLCV data to compute indicators natively.
        Uses 15-minute candles by default for intra-day mean reversion.
        The cycle interval should match (see cycle_interval_secs in config).
        """
        try:
            # yfinance mapping overrides for international/ETF tickers
            _MAPPING = {
                "VUSA": "VUSA.L",
                "EQQQ": "EQQQ.L",
                "IUSA": "IUSA.L",
            }
            
            yf_ticker = ticker.split("_")[0]
            yf_ticker = _MAPPING.get(yf_ticker, yf_ticker)
            
            df = yf.download(yf_ticker, period=period, interval=interval, progress=False)
            if df.empty:
                return df
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch data for {ticker}: {e}")
            return pd.DataFrame()

    def _generate_ml_features(self, df: pd.DataFrame, timeframe_label: str, benchmark_dfs: dict = None) -> pd.DataFrame:
        if len(df) < 50:
            return pd.DataFrame()
        df = df.copy()
        
        # 1. Price Momentum
        df['ret_1_bar'] = np.log(df['Close'] / df['Close'].shift(1))
        df['ret_5_bar'] = np.log(df['Close'] / df['Close'].shift(5))
        df['ret_20_bar'] = np.log(df['Close'] / df['Close'].shift(20))
        
        # 2. Moving Averages & Distances
        sma_20 = df['Close'].rolling(window=20).mean()
        sma_50 = df['Close'].rolling(window=50).mean()
        df['dist_sma_20'] = (df['Close'] - sma_20) / sma_20
        df['dist_sma_50'] = (df['Close'] - sma_50) / sma_50
        
        # 3. Oscillators
        df['rsi_14'] = ta.rsi(df['Close'], length=14)
        df['rsi_7'] = ta.rsi(df['Close'], length=7)
        
        macd = ta.macd(df['Close'], fast=12, slow=26, signal=9)
        if macd is not None and not macd.empty:
            df['macd'] = macd.iloc[:, 0]
            df['macd_hist'] = macd.iloc[:, 1]
            df['macd_signal'] = macd.iloc[:, 2]
            df['macd_trend'] = (df['macd_hist'] > df['macd_hist'].shift(1)).astype(int)
        else:
            df['macd'] = df['macd_hist'] = df['macd_signal'] = df['macd_trend'] = 0
            
        # 4. Volatility
        df['bar_range_pct'] = (df['High'] - df['Low']) / df['Close']
        df['volatility_20'] = df['ret_1_bar'].rolling(window=20).std()
        
        std_20 = df['Close'].rolling(window=20).std()
        upper_bb = sma_20 + (std_20 * 2)
        lower_bb = sma_20 - (std_20 * 2)
        df['bb_width'] = (upper_bb - lower_bb) / sma_20
        
        atr = ta.atr(df['High'], df['Low'], df['Close'], length=14)
        df['atr_pct'] = atr / df['Close']
        
        # 5. Volume
        if 'Volume' in df.columns:
            vol_sma_10 = df['Volume'].rolling(window=10).mean()
            df['vol_surge'] = df['Volume'] / vol_sma_10
        else:
            df['vol_surge'] = 1.0

        # Phase 4 SRS Calculation
        for bm_name in ['SPY', 'QQQ', 'IWM']:
            if benchmark_dfs and bm_name in benchmark_dfs and not benchmark_dfs[bm_name].empty:
                bm_close = benchmark_dfs[bm_name]['Close']
                aligned_close = bm_close.reindex(df.index, method='ffill')
                bm_ret = np.log(aligned_close / aligned_close.shift(1))
                df[f'ret_vs_{bm_name.lower()}'] = (df['ret_1_bar'] - bm_ret).fillna(0)
            else:
                df[f'ret_vs_{bm_name.lower()}'] = 0.0

        # Market Regime Context (SPY)
        if benchmark_dfs and 'SPY' in benchmark_dfs and not benchmark_dfs['SPY'].empty:
            spy_df = benchmark_dfs['SPY']
            spy_aligned = spy_df.reindex(df.index, method='ffill')
            
            spy_sma200 = spy_aligned['Close'].rolling(window=200).mean()
            df['spy_dist_sma200'] = ((spy_aligned['Close'] - spy_sma200) / spy_sma200).fillna(0)
            
            df['spy_rsi_14'] = ta.rsi(spy_aligned['Close'], length=14)
            df['spy_rsi_14'] = df['spy_rsi_14'].fillna(50.0)
            
            spy_ret = np.log(spy_aligned['Close'] / spy_aligned['Close'].shift(1))
            df['spy_volatility'] = spy_ret.rolling(window=20).std().fillna(0)
        else:
            df['spy_dist_sma200'] = 0.0
            df['spy_rsi_14'] = 50.0
            df['spy_volatility'] = 0.0

        # STRICT COLUMN LIST (Must match brain's training features exactly)
        feature_order = [
            'ret_1_bar', 'ret_5_bar', 'ret_20_bar', 'dist_sma_20', 'dist_sma_50',
            'rsi_14', 'rsi_7', 'macd', 'macd_signal', 'macd_hist', 'macd_trend',
            'bar_range_pct', 'volatility_20', 'bb_width', 'atr_pct', 'vol_surge',
            'ret_vs_spy', 'ret_vs_qqq', 'ret_vs_iwm',
            'spy_dist_sma200', 'spy_rsi_14', 'spy_volatility'
        ]
        
        feats = df[feature_order].copy()
        feats.columns = [f"{c}_{timeframe_label}" for c in feats.columns]
        return feats

    def analyze(self, ticker: str, quant_engine=None, benchmarks_1d=None, benchmarks_15m=None) -> dict:
        """
        Calculates indicators and returns a dictionary with current signals.
        If a quant_engine is provided containing an ML model, MTF features are engineered
        and an AI probability score is attached.
        """
        df_1d = self.get_historical_data(ticker, interval="1d", period="4mo")
        df = self.get_historical_data(ticker, interval="15m", period="10d")
        
        if df.empty or len(df) < 50 or df_1d.empty or len(df_1d) < 50:
            return {"signal": "NEUTRAL", "reason": "Not enough data"}

        # Calculate Indicators using pandas_ta
        # Bollinger Bands
        bbands = ta.bbands(df['Close'], length=self.bb_length, std=self.bb_std)
        if bbands is None or bbands.empty:
            return {"signal": "NEUTRAL", "reason": "Indicator calc failed"}
            
        df = pd.concat([df, bbands], axis=1)
        
        # RSI
        df['RSI'] = ta.rsi(df['Close'], length=self.rsi_length)
        
        # ATR
        df['ATR'] = ta.atr(df['High'], df['Low'], df['Close'], length=14)
        
        # Extract latest candle for signal generation
        latest = df.iloc[-1]
        previous = df.iloc[-2]
        
        # Dynamically grab the BB column names since `pandas_ta` float formatting can trigger KeyError
        bbl_col = next((c for c in bbands.columns if c.startswith('BBL_')), None)
        bbm_col = next((c for c in bbands.columns if c.startswith('BBM_')), None)
        bbu_col = next((c for c in bbands.columns if c.startswith('BBU_')), None)
        
        current_price  = float(latest['Close'])
        lower_band     = float(latest[bbl_col])  if bbl_col else current_price
        basis          = float(latest[bbm_col])  if bbm_col else current_price
        upper_band     = float(latest[bbu_col])  if bbu_col else (basis + (basis - lower_band))
        rsi            = float(latest['RSI'])
        atr            = float(latest['ATR'])

        # Regime Detection (SMA 100 on 15m)
        df['SMA100'] = ta.sma(df['Close'], length=100)
        sma100 = df['SMA100'].iloc[-1]
        if pd.isna(sma100):
            sma100 = current_price  # fallback
        regime = "BULLISH" if current_price > sma100 else "BEARISH"

        diag = f"[Math: P={current_price:.2f}, RSI={rsi:.2f}, ATR={atr:.2f}, Regime={regime}]"

        # Previous candle values (for crossover logic)
        prev_close      = float(previous['Close'])
        lower_band_prev = float(previous[bbl_col]) if bbl_col else prev_close

        # Black Swan Volatility Filter
        candle_range = latest['High'] - latest['Low']
        if candle_range > 3 * atr:
            logger.warning(f"BLACK SWAN AVOIDED: {ticker} candle range {candle_range:.2f} > 3x ATR ({atr:.2f})")
            return {"signal": "BLOCK", "reason": f"High Volatility Black Swan {diag}"}

        # Volume Confirmation Filter
        if 'Volume' in df.columns:
            avg_volume = df['Volume'].rolling(20).mean().iloc[-1]
            min_vol_pct = self.volume_min_pct
            if avg_volume and avg_volume > 0 and latest['Volume'] < avg_volume * min_vol_pct:
                logger.info(
                    f"[{ticker}] Volume too low ({latest['Volume']:.0f} < "
                    f"{avg_volume * min_vol_pct:.0f} = {min_vol_pct*100:.0f}% of avg). Skipping signal."
                )
                target_tp = basis if self.tp_target_mode != "Upper Band" else upper_band
                return {
                    "signal": "WAIT",
                    "price": current_price,
                    "target_tp": target_tp,
                    "reason": f"Low volume – no conviction {diag}"
                }

        # --- AI ML Inference (Node 3 Execution) ---
        ai_win_prob = 0.50
        if quant_engine and quant_engine.is_ai_active():
            try:
                # Generate features using the STRICT 38-feature order
                feats_15m = self._generate_ml_features(df, "15m", benchmark_dfs=benchmarks_15m)
                feats_1d = self._generate_ml_features(df_1d, "1d", benchmark_dfs=benchmarks_1d)
                
                if feats_15m.empty or feats_1d.empty:
                    raise ValueError("Empty features generated")

                # Get the absolute most recent 15m features
                latest_15m = feats_15m.iloc[-1:].copy()
                
                # CRITICAL: To prevent lookahead bias, we use "yesterday's" completed daily candle
                latest_1d = feats_1d.iloc[-2:-1].copy()
                
                latest_15m.reset_index(drop=True, inplace=True)
                latest_1d.reset_index(drop=True, inplace=True)
                
                feature_df = pd.concat([latest_15m, latest_1d], axis=1)
                feature_df.fillna(0, inplace=True)
                
                ai_win_prob = quant_engine.get_win_probability(feature_df)
                
                srs_spy = feature_df["ret_vs_spy_1d"].iloc[0]
                diag += f" [SRS vs SPY 1d: {srs_spy:+.4f}] [AI Prob: {ai_win_prob*100:.1f}%]"
            except Exception as e:
                logger.warning(f"[{ticker}] AI Inference failed: {e}")

        # ── Entry Logic ───────────────────────────────────────────────────────
        # AI OVERRIDE: If the Deep Learning model is highly confident AND price is
        # below the middle band (not at a local peak), bypass the strict BB crossover.
        # We keep the "below basis" gate to prevent buying at the top of a range.
        if quant_engine and quant_engine.is_ai_active() and ai_win_prob >= 0.65:
            if current_price < basis:  # Soft gate: must not be above the midline
                # Need a TP target for the bot to execute against
                target_tp = upper_band if self.tp_target_mode != "Fixed: Mean (Middle BB)" else basis
                return {
                    "signal":          "BUY",
                    "price":           current_price,
                    "target_tp":       target_tp,
                    "rsi":             rsi,
                    "bb_pct_below":    0.0,
                    "atr":             atr,
                    "ai_win_prob":     ai_win_prob,
                    "fresh_break":     True,
                    "reason": f"🤖 AI High Conviction Buy (Prob: {ai_win_prob*100:.1f}%, Price below basis) {diag}"
                }
            else:
                logger.info(
                    f"[{ticker}] 🤖 AI High Conviction ({ai_win_prob*100:.1f}%) but price "
                    f"({current_price:.4f}) >= basis ({basis:.4f}). Waiting for pullback."
                )

        # Require a FRESH crossover below the lower band (not just 'already below').
        # Previous candle must have closed AT or ABOVE the lower band, and the
        # current candle must close BELOW it.  This prevents chasing stocks that
        # have been bleeding below the band for several candles already.
        fresh_break = (prev_close >= lower_band_prev) and (current_price < lower_band)

        if fresh_break and rsi < self.rsi_threshold:
            # Smart Regime Filter
            if self.smart_regime_enabled and regime == "BEARISH":
                logger.info(f"[{ticker}] BUY skipped due to Smart Regime Filter (Regime=BEARISH, P={current_price:.2f} < SMA100={sma100:.2f})")
                return {"signal": "WAIT", "price": current_price, "ai_win_prob": ai_win_prob, "reason": f"Regime filter blocked {diag}"}

            bb_pct_below = ((lower_band - current_price) / lower_band) * 100
            
            # Take Profit Optimisation
            if self.tp_target_mode == "Upper Band":
                target_tp = upper_band
            elif self.tp_target_mode == "Dynamic (Auto-Switch)":
                target_tp = upper_band if regime == "BULLISH" else basis
            else:
                target_tp = basis

            return {
                "signal":          "BUY",
                "price":           current_price,
                "target_tp":       target_tp,
                "rsi":             rsi,
                "bb_pct_below":    float(bb_pct_below),
                "atr":             atr,
                "ai_win_prob":     ai_win_prob,
                "fresh_break":     True,
                "reason": (
                    f"Fresh BB lower cross: prev={prev_close:.4f}>={lower_band_prev:.4f}, "
                    f"now={current_price:.4f}<{lower_band:.4f} | RSI={rsi:.2f} | TP Target={target_tp:.2f} {diag}"
                )
            }

        # Exit Logic — price has returned to or above the midline
        if current_price >= basis:
            return {
                "signal": "SELL",
                "price":  current_price,
                "ai_win_prob": ai_win_prob,
                "reason": f"Price returned to basis ({basis:.4f}) {diag}"
            }

        return {
            "signal": "WAIT", 
            "price": current_price, 
            "ai_win_prob": ai_win_prob,
            "reason": f"No conditions met {diag}"
        }

    def get_current_atr(self, ticker: str, multiplier: float = 1.0) -> float:
        """
        Returns the latest ATR value for the ticker, scaled by multiplier.
        Used by the bot to calculate an ATR-aware stop-loss for imported positions.
        Returns 0.0 if data is unavailable so the caller falls back to pct-based SL.
        """
        df = self.get_historical_data(ticker)
        if df.empty or len(df) < 15:
            return 0.0
        try:
            atr_series = ta.atr(df['High'], df['Low'], df['Close'], length=14)
            latest_atr = float(atr_series.iloc[-1])
            # Guard against NaN
            if latest_atr != latest_atr:
                return 0.0
            return round(latest_atr * multiplier, 4)
        except Exception as e:
            logger.warning(f"ATR fetch failed for {ticker}: {e}")
            return 0.0
