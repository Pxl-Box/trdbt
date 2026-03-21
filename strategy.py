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
            yf_ticker = ticker.split("_")[0]
            df = yf.download(yf_ticker, period=period, interval=interval, progress=False)
            if df.empty:
                return df
            if isinstance(df.columns, pd.MultiIndex):
                df.columns = df.columns.droplevel(1)
            return df
        except Exception as e:
            logger.error(f"Failed to fetch data for {ticker}: {e}")
            return pd.DataFrame()

    def analyze(self, ticker: str, quant_engine=None) -> dict:
        """
        Calculates indicators and returns a dictionary with current signals.
        If a quant_engine is provided containing an ML model, features are engineered
        and an AI probability score is attached.
        """
        df = self.get_historical_data(ticker, interval="1d", period="3mo") # Need 1d for ML features, at least 60 days
        if df.empty or len(df) < 50:
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
                return {"signal": "WAIT", "price": current_price, "reason": f"Low volume – no conviction {diag}"}

        # --- AI ML Inference (Node 3 Execution) ---
        ai_win_prob = 0.50
        if quant_engine and quant_engine.is_ai_active():
            try:
                # Calculate required ML features for the very last row
                features = {}
                features['ret_1d'] = np.log(current_price / df['Close'].iloc[-2]) if len(df)>1 else 0
                features['ret_5d'] = np.log(current_price / df['Close'].iloc[-6]) if len(df)>5 else 0
                features['ret_20d'] = np.log(current_price / df['Close'].iloc[-21]) if len(df)>20 else 0
                
                sma20 = df['Close'].rolling(20).mean().iloc[-1]
                sma50 = df['Close'].rolling(50).mean().iloc[-1]
                features['dist_sma_20'] = (current_price - sma20) / sma20 if sma20 else 0
                features['dist_sma_50'] = (current_price - sma50) / sma50 if sma50 else 0
                
                features['rsi_14'] = rsi
                macd = ta.macd(df['Close'], fast=12, slow=26, signal=9)
                features['macd_hist'] = float(macd.iloc[-1, 1]) if macd is not None and not macd.empty else 0
                
                features['daily_range_pct'] = (latest['High'] - latest['Low']) / current_price
                log_ret = np.log(df['Close'] / df['Close'].shift(1))
                features['volatility_20d'] = log_ret.rolling(20).std().iloc[-1]
                
                features['vol_surge'] = latest['Volume'] / avg_volume if avg_volume > 0 else 1.0

                # --- Big Brain Features ---
                try:
                    rsi_7_series = ta.rsi(df['Close'], length=7)
                    features['rsi_7'] = float(rsi_7_series.iloc[-1]) if rsi_7_series is not None and not rsi_7_series.empty else rsi
                except Exception:
                    features['rsi_7'] = rsi

                macd_hist_prev = float(macd.iloc[-2, 1]) if macd is not None and len(macd) > 1 else 0
                features['macd_trend'] = 1 if features['macd_hist'] > macd_hist_prev else 0
                
                features['bb_width'] = (upper_band - lower_band) / basis if basis > 0 else 0
                features['atr_pct'] = atr / current_price if current_price else 0

                # Must match exact DataFrame shape expected by the model
                feature_df = pd.DataFrame([features])
                
                # Drop NaNs by filling with 0 (safe fallback)
                feature_df.fillna(0, inplace=True)
                
                ai_win_prob = quant_engine.get_win_probability(feature_df)
                diag += f" [AI Prob: {ai_win_prob*100:.1f}%]"
            except Exception as e:
                logger.warning(f"[{ticker}] AI Inference feature generation failed: {e}")

        # ── Entry Logic ───────────────────────────────────────────────────────
        # AI OVERRIDE: If the Deep Learning model is highly confident, bypass dumb math
        if quant_engine and quant_engine.is_ai_active() and ai_win_prob >= 0.65:
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
                "reason": f"🤖 AI High Conviction Buy (Prob: {ai_win_prob*100:.1f}%) {diag}"
            }

        # Require a FRESH crossover below the lower band (not just 'already below').
        # Previous candle must have closed AT or ABOVE the lower band, and the
        # current candle must close BELOW it.  This prevents chasing stocks that
        # have been bleeding below the band for several candles already.
        fresh_break = (prev_close >= lower_band_prev) and (current_price < lower_band)

        if fresh_break and rsi < self.rsi_threshold:
            # Smart Regime Filter
            if self.smart_regime_enabled and regime == "BEARISH":
                logger.info(f"[{ticker}] BUY skipped due to Smart Regime Filter (Regime=BEARISH, P={current_price:.2f} < SMA100={sma100:.2f})")
                return {"signal": "WAIT", "price": current_price, "reason": f"Regime filter blocked {diag}"}

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
                "reason": f"Price returned to basis ({basis:.4f}) {diag}"
            }

        return {"signal": "WAIT", "price": current_price, "reason": f"No conditions met {diag}"}

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
