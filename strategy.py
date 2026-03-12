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
        """
        try:
            # yfinance tickers might require slight formatting vs Trading 212.
            # e.g., Trading 212 uses SPY_US_EQ, yfinance uses SPY.
            yf_ticker = ticker.split("_")[0] 
            df = yf.download(yf_ticker, period=period, interval=interval, progress=False)
            if df.empty:
                return df
                
            # Flatten potential MultiIndex if yfinance returns it
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
        
        current_price = latest['Close']
        lower_band = latest[bbl_col] if bbl_col else current_price
        basis = latest[bbm_col] if bbm_col else current_price # Middle band
        rsi = latest['RSI']
        atr = latest['ATR']
        
        # Black Swan Volatility Filter
        # If the latest candle's range is extremely large (> 3x ATR), assume panic
        candle_range = latest['High'] - latest['Low']
        if candle_range > 3 * atr:
            logger.warning(f"BLACK SWAN AVOIDED: {ticker} candle range {candle_range:.2f} > 3x ATR ({atr:.2f})")
            return {"signal": "BLOCK", "reason": "High Volatility Black Swan"}

        # Entry Logic: Price < Lower Band AND RSI < threshold
        if current_price < lower_band and rsi < self.rsi_threshold:
            return {
                "signal": "BUY",
                "price": current_price,
                "target_tp": basis, # Take profit at the mean
                "reason": f"Price ({current_price:.2f}) < BB Lower ({lower_band:.2f}) & RSI ({rsi:.2f}) < {self.rsi_threshold}"
            }
            
        # Exit Logic (If currently holding) is handled when holding. The strategy only tells us if it crossed the basis.
        if current_price >= basis:
            return {
                "signal": "SELL",
                "price": current_price,
                "reason": f"Price touched/crossed basis ({basis:.2f})"
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
