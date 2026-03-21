import logging
import os
import json
from pathlib import Path
import pandas as pd
import numpy as np

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Paths
def load_node_config():
    root_dir = Path(__file__).parent.parent
    config_path = root_dir / "node_config.json"
    if config_path.exists():
        try:
            with open(config_path, "r") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Error loading node_config.json: {e}")
    return {}

NODE_CONFIG = load_node_config()
SHARED_DRIVE_DIR = NODE_CONFIG.get("shared_drive_path", r"D:\trd-data")

BASE_DIR = Path(SHARED_DRIVE_DIR) if SHARED_DRIVE_DIR else Path(__file__).parent
RAW_DATA_DIR = BASE_DIR / "raw_data"
PROCESSED_DATA_DIR = BASE_DIR / "processed_data"
PROCESSED_DATA_DIR.mkdir(parents=True, exist_ok=True)

def calculate_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    """Calculates Relative Strength Index."""
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
    return 100 - (100 / (1 + rs))

def calculate_macd(series: pd.Series, fast: int = 12, slow: int = 26, signal: int = 9) -> pd.DataFrame:
    """Calculates MACD and Signal Line."""
    ema_fast = series.ewm(span=fast, adjust=False).mean()
    ema_slow = series.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    
    return pd.DataFrame({
        'macd': macd_line,
        'macd_signal': signal_line,
        'macd_hist': macd_line - signal_line
    })

def generate_base_features(df: pd.DataFrame, timeframe_label: str) -> pd.DataFrame:
    """
    Generates technical indicators for a given OHLCV dataframe,
    appending the timeframe label (e.g., '_15m' or '_1d') to every column.
    """
    if len(df) < 50:
        return pd.DataFrame() # Not enough data
        
    df = df.copy()
    
    # 1. Price Momentum Features
    # Since intervals are different, "1d" means 1 candle.
    df['ret_1_bar'] = np.log(df['close'] / df['close'].shift(1))
    df['ret_5_bar'] = np.log(df['close'] / df['close'].shift(5))
    df['ret_20_bar'] = np.log(df['close'] / df['close'].shift(20))
    
    # 2. Moving Averages & Distances
    sma_20 = df['close'].rolling(window=20).mean()
    sma_50 = df['close'].rolling(window=50).mean()
    
    df['dist_sma_20'] = (df['close'] - sma_20) / sma_20
    df['dist_sma_50'] = (df['close'] - sma_50) / sma_50
    
    # 3. Oscillators
    df['rsi_14'] = calculate_rsi(df['close'])
    df['rsi_7'] = calculate_rsi(df['close'], period=7)
    
    macd_df = calculate_macd(df['close'])
    df = pd.concat([df, macd_df], axis=1)
    df['macd_trend'] = (df['macd_hist'] > df['macd_hist'].shift(1)).astype(int)
    
    # 4. Volatility (High/Low Range & Bands)
    df['bar_range_pct'] = (df['high'] - df['low']) / df['close']
    df['volatility_20'] = df['ret_1_bar'].rolling(window=20).std()
    
    std_20 = df['close'].rolling(window=20).std()
    upper_bb = sma_20 + (std_20 * 2)
    lower_bb = sma_20 - (std_20 * 2)
    df['bb_width'] = (upper_bb - lower_bb) / sma_20
    
    # ATR Pct
    high_low = df['high'] - df['low']
    high_close = np.abs(df['high'] - df['close'].shift())
    low_close = np.abs(df['low'] - df['close'].shift())
    tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
    atr = tr.rolling(window=14).mean()
    df['atr_pct'] = atr / df['close']
    
    # 5. Volume Features
    vol_sma_10 = df['volume'].rolling(window=10).mean()
    df['vol_surge'] = df['volume'] / vol_sma_10
    
    # Drop original OHLCV columns -- we don't want absolute prices fed to the ML model!
    feats = df.drop(columns=['open', 'high', 'low', 'close', 'volume', 'adj_close'], errors='ignore')
    
    # Rename all columns with the timeframe suffix
    feats.columns = [f"{c}_{timeframe_label}" for c in feats.columns]
    
    return feats

def process_and_stitch_ticker(ticker: str):
    """
    Loads both the 15m and 1d raw data for a ticker.
    Calculates features for both timeframes.
    Shifts the 1d features to prevent look-ahead bias.
    Merges them using merge_asof.
    """
    file_1d = RAW_DATA_DIR / f"{ticker}_raw_1d.parquet"
    file_15m = RAW_DATA_DIR / f"{ticker}_raw_15m.parquet"
    
    if not file_1d.exists() or not file_15m.exists():
        logger.warning(f"[{ticker}] Missing one or both timeframes. Skipping.")
        return False
        
    try:
        df_1d = pd.read_parquet(file_1d).sort_index()
        df_15m = pd.read_parquet(file_15m).sort_index()
        
        feats_1d = generate_base_features(df_1d, "1d")
        feats_15m = generate_base_features(df_15m, "15m")
        
        if feats_1d.empty or feats_15m.empty:
            logger.warning(f"[{ticker}] Insufficient data for feature generation.")
            return False
            
        # CRITICAL: Prevent Look-Ahead Bias on the Daily Data.
        # Yahoo Finance marks daily candles at midnight.
        # We only know the true Close price at the END of that day.
        # So we MUST shift the daily features forward by 1 trading day!
        feats_1d = feats_1d.shift(1)
        feats_1d.dropna(inplace=True)
        
        # We need timezone matching for merge_asof
        if df_15m.index.tzinfo != feats_1d.index.tzinfo:
            logger.warning(f"[{ticker}] Tzinfo mismatch. Aligning prior to stitch.")
            if df_15m.index.tz is None:
                df_15m.index = df_15m.index.tz_localize('UTC')
            if feats_1d.index.tz is None:
                feats_1d.index = feats_1d.index.tz_localize('UTC')
                
        # Merge: For every 15m row, find the most recent 1d row (looking backward)
        stitched = pd.merge_asof(
            feats_15m, 
            feats_1d, 
            left_index=True, 
            right_index=True, 
            direction='backward'
        )
        
        # RE-ATTACH THE TARGET LABEL
        # The AI needs to predict 15m returns.
        # Target: Is the profit > 1% within the next 26 candles (1 full day)?
        future_return = (df_15m['close'].shift(-26) - df_15m['close']) / df_15m['close']
        stitched['target_win'] = (future_return > 0.01).astype(int)
        
        # Cleanup
        stitched.dropna(inplace=True)
        
        if stitched.empty:
            logger.warning(f"[{ticker}] Stitched dataframe is empty.")
            return False
            
        output_file = PROCESSED_DATA_DIR / f"{ticker}_features.parquet"
        stitched.to_parquet(output_file, engine='pyarrow')
        return True
        
    except Exception as e:
        logger.error(f"[{ticker}] Error stitching data: {e}", exc_info=True)
        return False

def process_all_files():
    """Finds all unique tickers in raw_data and stitches their timeframes."""
    logger.info("=== Starting Data Lake MTF Feature Stitching ===")
    
    if not RAW_DATA_DIR.exists():
        logger.error(f"Raw data directory not found at {RAW_DATA_DIR}")
        return
        
    raw_files = list(RAW_DATA_DIR.glob("*.parquet"))
    tickers = set()
    for f in raw_files:
        parts = f.stem.split("_raw_")
        if len(parts) == 2:
            tickers.add(parts[0])
            
    logger.info(f"Found {len(tickers)} unique tickers for MTF processing.")
    
    success_count = 0
    for ticker in tickers:
        if process_and_stitch_ticker(ticker):
            success_count += 1
            
    logger.info(f"=== MTF Feature Engineering Complete. Stitched {success_count}/{len(tickers)} tickers. ===")

if __name__ == "__main__":
    process_all_files()
