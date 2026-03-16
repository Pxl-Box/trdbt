import json
import logging
import os
from pathlib import Path
import yfinance as yf
import pandas as pd
from datetime import datetime

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Paths
ROOT_DIR = Path(__file__).parent.parent
TICKERS_FILE = ROOT_DIR / "trdbt_tickers.json"

def load_node_config():
    config_path = ROOT_DIR / "node_config.json"
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
RAW_DATA_DIR.mkdir(parents=True, exist_ok=True)

def load_tickers() -> list:
    """Loads all tickers from the central config file."""
    if not TICKERS_FILE.exists():
        logger.error(f"Tickers file not found at {TICKERS_FILE}")
        return []
    
    try:
        with open(TICKERS_FILE, "r") as f:
            data = json.load(f)
            
        all_tickers = []
        for sector, tickers in data.items():
            all_tickers.extend(tickers)
        return list(set(all_tickers)) # Deduplicate
    except Exception as e:
        logger.error(f"Failed to load tickers: {e}")
        return []

def download_ticker_history(ticker: str, period: str = "max", interval: str = "1d"):
    """
    Downloads full historical data for a ticker and saves it to Parquet.
    We use Parquet because it is vastly faster and smaller than CSV for ML workflows.
    """
    logger.info(f"[{ticker}] Downloading history. Period: {period}, Interval: {interval}")
    
    try:
        # T212 format usually strips endings or adds them, but yfinance needs standard.
        # If the ticker has a strange T212 suffix, we might need to strip it.
        # For our system, the json usually holds standard YF tickers.
        yf_ticker = yf.Ticker(ticker)
        df = yf_ticker.history(period=period, interval=interval, auto_adjust=True)
        
        if df.empty:
            logger.warning(f"[{ticker}] No data returned from Yahoo Finance.")
            return False
            
        # Clean column names (remove multi-index if present)
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        
        # Save to Parquet
        output_file = RAW_DATA_DIR / f"{ticker}_raw_{interval}.parquet"
        
        # If the index is a timezone-aware datetime, convert it to UTC for consistency
        if isinstance(df.index, pd.DatetimeIndex):
            if df.index.tz is not None:
                df.index = df.index.tz_convert('UTC')
            else:
                df.index = df.index.tz_localize('UTC')
                
        df.to_parquet(output_file, engine='pyarrow')
        logger.info(f"[{ticker}] Saved {len(df)} rows to {output_file.name}")
        return True
        
    except Exception as e:
        logger.error(f"[{ticker}] Failed to download data: {e}")
        return False

def main():
    logger.info("=== Starting Data Lake Ingestion ===")
    tickers = load_tickers()
    
    if not tickers:
        logger.error("No tickers to process. Exiting.")
        return
        
    logger.info(f"Found {len(tickers)} tickers to process.")
    
    success_count = 0
    for ticker in tickers:
        success = download_ticker_history(ticker, period="10y", interval="1d")
        if success:
            success_count += 1
            
    logger.info(f"=== Ingestion Complete. Successfully downloaded {success_count}/{len(tickers)} tickers. ===")

if __name__ == "__main__":
    main()
