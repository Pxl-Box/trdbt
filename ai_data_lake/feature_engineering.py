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

def calculate_features(df: pd.DataFrame) -> pd.DataFrame:
    """
    Takes raw OHLCV data and engineers dozens of technical/statistical features
    for the Machine Learning model. Crucially, ensures NO Look-Ahead bias.
    """
    if len(df) < 50:
        return pd.DataFrame() # Not enough data
        
    df = df.copy()
    
    # 1. Price Momentum Features (Log Returns to normalize)
    df['ret_1d'] = np.log(df['close'] / df['close'].shift(1))
    df['ret_5d'] = np.log(df['close'] / df['close'].shift(5))
    df['ret_20d'] = np.log(df['close'] / df['close'].shift(20))
    
    # 2. Moving Averages & Distances
    df['sma_20'] = df['close'].rolling(window=20).mean()
    df['sma_50'] = df['close'].rolling(window=50).mean()
    df['sma_200'] = df['close'].rolling(window=200).mean()
    
    # Distance from Moving Averages (stationary features are better for ML)
    df['dist_sma_20'] = (df['close'] - df['sma_20']) / df['sma_20']
    df['dist_sma_50'] = (df['close'] - df['sma_50']) / df['sma_50']
    
    # 3. Oscillators
    df['rsi_14'] = calculate_rsi(df['close'])
    
    macd_df = calculate_macd(df['close'])
    df = pd.concat([df, macd_df], axis=1)
    
    # 4. Volatility (High/Low Range)
    df['daily_range_pct'] = (df['high'] - df['low']) / df['close']
    df['volatility_20d'] = df['ret_1d'].rolling(window=20).std()
    
    # 5. Volume Features
    df['vol_sma_10'] = df['volume'].rolling(window=10).mean()
    df['vol_surge'] = df['volume'] / df['vol_sma_10']
    
    # --- The Target Label (What we want to predict) ---
    # Example: Will the price be 3% higher 5 days from now?
    # THIS IS THE ONLY PLACE WE USE .shift(-N) (Looking into the future).
    # We must explicitly drop this column before feeding it to the AI during live inference!
    future_return = (df['close'].shift(-5) - df['close']) / df['close']
    df['target_win_5d'] = (future_return > 0.03).astype(int) # 1 if >3% profit, else 0
    
    # Drop rows with NaN values created by rolling windows or forward shifts
    df.dropna(inplace=True)
    
    return df

def process_all_files():
    """Iterates through raw_data, calculates features, and saves to processed_data"""
    logger.info("=== Starting Data Lake Feature Engineering ===")
    
    if not RAW_DATA_DIR.exists():
        logger.error(f"Raw data directory not found at {RAW_DATA_DIR}")
        return
        
    raw_files = list(RAW_DATA_DIR.glob("*.parquet"))
    logger.info(f"Found {len(raw_files)} raw data files.")
    
    success_count = 0
    for file_path in raw_files:
        try:
            df = pd.read_parquet(file_path)
            
            # Ensure index is sorted chronologically
            df.sort_index(inplace=True)
            
            features_df = calculate_features(df)
            
            if not features_df.empty:
                output_file = PROCESSED_DATA_DIR / f"{file_path.stem}_features.parquet"
                features_df.to_parquet(output_file, engine='pyarrow')
                success_count += 1
                logger.debug(f"Process [{file_path.name}]: {len(features_df)} ML-ready rows.")
            else:
                logger.warning(f"[{file_path.name}] Insufficient data for feature engineering.")
                
        except Exception as e:
            logger.error(f"Error processing {file_path.name}: {e}")
            
    logger.info(f"=== Feature Engineering Complete. Processed {success_count}/{len(raw_files)} files. ===")

if __name__ == "__main__":
    process_all_files()
