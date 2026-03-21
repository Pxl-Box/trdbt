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
        # Priority 1: Use the combined_list if present
        if "combined_list" in data:
            all_tickers = data["combined_list"]
        # Priority 2: Iterate through categories if combined_list is missing
        elif "categories" in data:
            for sector, tickers in data["categories"].items():
                if isinstance(tickers, list):
                    all_tickers.extend(tickers)
        
        return list(set(all_tickers)) # Deduplicate
    except Exception as e:
        logger.error(f"Failed to load tickers: {e}")
        return []

def clean_ticker(ticker: str) -> str:
    """
    Cleans Trading 212 specific suffixes from tickers to make them Yahoo-compatible.
    Example: IBIT_US_EQ -> IBIT
    """
    if not isinstance(ticker, str):
        return ticker
        
    # Common T212 suffixes
    suffixes = ["_US_EQ", "_UK_EQ", "_LSE_EQ", "_DE_EQ"]
    cleaned = ticker
    for s in suffixes:
        cleaned = cleaned.replace(s, "")
    
    return cleaned.strip()

def download_ticker_history(ticker: str, period: str = "max", interval: str = "1d"):
    """
    Downloads full historical data for a ticker and saves it to Parquet.
    We use Parquet because it is vastly faster and smaller than CSV for ML workflows.
    """
    logger.info(f"[{ticker}] Processing history. Period: {period}, Interval: {interval}")
    
    try:
        # Step 1: Clean the ticker (remove T212 suffixes like _US_EQ)
        yf_symbol = clean_ticker(ticker)
        
        if yf_symbol != ticker:
            logger.info(f"[{ticker}] Cleaned to Yahoo symbol: {yf_symbol}")
            
        yf_ticker = yf.Ticker(yf_symbol)
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

BENCHMARKS_DIR = BASE_DIR / "benchmarks"
BENCHMARKS_DIR.mkdir(parents=True, exist_ok=True)

# Market benchmarks used for Sector-Relative Strength (SRS) feature calculation.
# These are always downloaded regardless of the user's ticker list.
BENCHMARK_TICKERS = ["SPY", "QQQ", "IWM"]

def download_benchmark_history(ticker: str, period: str = "2y", interval: str = "1d"):
    """Downloads a benchmark index and saves to the benchmarks/ subfolder."""
    logger.info(f"[BENCHMARK] Fetching {ticker} ({interval}, {period})...")
    try:
        df = yf.Ticker(ticker).history(period=period, interval=interval, auto_adjust=True)
        if df.empty:
            logger.warning(f"[BENCHMARK] No data for {ticker}.")
            return False
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        if isinstance(df.index, pd.DatetimeIndex):
            df.index = df.index.tz_convert('UTC') if df.index.tz else df.index.tz_localize('UTC')
        output_file = BENCHMARKS_DIR / f"{ticker}_benchmark_{interval}.parquet"
        df.to_parquet(output_file, engine='pyarrow')
        logger.info(f"[BENCHMARK] {ticker}: Saved {len(df)} rows to {output_file.name}")
        return True
    except Exception as e:
        logger.error(f"[BENCHMARK] Failed to fetch {ticker}: {e}")
        return False

def main():
    logger.info("=== Starting Data Lake Ingestion ===")

    # ── Phase 1: Always download market benchmarks first ──────────────────
    logger.info("--- Fetching Market Benchmarks (SPY, QQQ, IWM) ---")
    for bm in BENCHMARK_TICKERS:
        download_benchmark_history(bm, period="2y", interval="1d")
        download_benchmark_history(bm, period="60d", interval="15m")

    # ── Phase 2: Fetch ticker histories ───────────────────────────────────
    tickers = load_tickers()
    if not tickers:
        logger.error("No tickers to process. Exiting.")
        return

    logger.info(f"Found {len(tickers)} tickers to process for MTF (Daily + 15m).")
    success_count = 0
    for ticker in tickers:
        logger.info(f"--- Fetching Macro (1d) and Micro (15m) for {ticker} ---")
        success_1d  = download_ticker_history(ticker, period="2y",  interval="1d")
        success_15m = download_ticker_history(ticker, period="60d", interval="15m")
        if success_1d and success_15m:
            success_count += 1

    logger.info(f"=== Ingestion Complete. Successfully downloaded MTF data for {success_count}/{len(tickers)} tickers. ===")

if __name__ == "__main__":
    main()
