import pandas as pd
import pandas_ta as ta
import yfinance as yf
import logging

logger = logging.getLogger(__name__)

class MeanReversionStrategy:
    """
    Implements the core strategic logic for the trading bot based on Bollinger Bands,
    RSI, and an ATR Black Swan filter. Uses yfinance for reliable market data.
    """
    def __init__(self, bb_length=20, bb_std=2.0, rsi_length=14, rsi_threshold=30):
        self.bb_length = bb_length
        self.bb_std = bb_std
        self.rsi_length = rsi_length
        self.rsi_threshold = rsi_threshold

    def get_historical_data(self, ticker: str, interval="15m", period="5d") -> pd.DataFrame:
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

    def analyze(self, ticker: str) -> dict:
        """
        Calculates indicators and returns a dictionary with current signals.
        """
        df = self.get_historical_data(ticker)
        if df.empty or len(df) < max(self.bb_length, self.rsi_length):
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
        
        current_price  = float(latest['Close'])
        lower_band     = float(latest[bbl_col])  if bbl_col else current_price
        basis          = float(latest[bbm_col])  if bbm_col else current_price
        rsi            = float(latest['RSI'])
        atr            = float(latest['ATR'])

        # Previous candle values (for crossover logic)
        prev_close      = float(previous['Close'])
        lower_band_prev = float(previous[bbl_col]) if bbl_col else prev_close

        # Black Swan Volatility Filter
        candle_range = latest['High'] - latest['Low']
        if candle_range > 3 * atr:
            logger.warning(f"BLACK SWAN AVOIDED: {ticker} candle range {candle_range:.2f} > 3x ATR ({atr:.2f})")
            return {"signal": "BLOCK", "reason": "High Volatility Black Swan"}

        # Volume Confirmation Filter
        if 'Volume' in df.columns:
            avg_volume = df['Volume'].rolling(20).mean().iloc[-1]
            min_vol_pct = getattr(self, 'volume_min_pct', 0.8)
            if avg_volume and avg_volume > 0 and latest['Volume'] < avg_volume * min_vol_pct:
                logger.info(
                    f"[{ticker}] Volume too low ({latest['Volume']:.0f} < "
                    f"{avg_volume * min_vol_pct:.0f} = {min_vol_pct*100:.0f}% of avg). Skipping signal."
                )
                return {"signal": "WAIT", "price": current_price, "reason": "Low volume – no conviction"}

        # ── Entry Logic ───────────────────────────────────────────────────────
        # Require a FRESH crossover below the lower band (not just 'already below').
        # Previous candle must have closed AT or ABOVE the lower band, and the
        # current candle must close BELOW it.  This prevents chasing stocks that
        # have been bleeding below the band for several candles already.
        fresh_break = (prev_close >= lower_band_prev) and (current_price < lower_band)

        if fresh_break and rsi < self.rsi_threshold:
            bb_pct_below = ((lower_band - current_price) / lower_band) * 100
            return {
                "signal":          "BUY",
                "price":           current_price,
                "target_tp":       basis,
                "rsi":             rsi,
                "bb_pct_below":    float(bb_pct_below),
                "atr":             atr,
                "fresh_break":     True,
                "reason": (
                    f"Fresh BB lower cross: prev={prev_close:.4f}>={lower_band_prev:.4f}, "
                    f"now={current_price:.4f}<{lower_band:.4f} | RSI={rsi:.2f}"
                )
            }

        # Exit Logic — price has returned to or above the midline
        if current_price >= basis:
            return {
                "signal": "SELL",
                "price":  current_price,
                "reason": f"Price returned to basis ({basis:.4f})"
            }

        return {"signal": "WAIT", "price": current_price, "reason": "No conditions met"}

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
