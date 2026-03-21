"""
ai_discovery.py — Phase 4: AI Auto-Discovery Engine

Scans Yahoo Finance's "Most Active" + "Day Gainers" screeners every cycle.
For each discovered ticker, it fires the live AI inference pipeline and
checks whether the Sector-Relative Strength (and overall win prob) meets the
bar.  Any ticker that passes is temporarily added to the bot's watchlist for
one full trading day.

Run this script standalone, or call `run_discovery_cycle()` from a scheduler.
"""
import json
import logging
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path

import requests
import yfinance as yf
import pandas as pd
import numpy as np

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).parent
TICKERS_FILE = ROOT_DIR / "trdbt_tickers.json"
CONFIG_FILE  = ROOT_DIR / "config.json"

# Thresholds
AI_WIN_THRESHOLD  = 0.65   # Require at least 65% win probability
SRS_THRESHOLD     = 0.002  # Must outperform SPY by at least 0.2% on the day
DISCOVERY_TTL_HRS = 24     # How long to keep a discovered ticker

SCRENER_URLS = {
    "day_gainers":  "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=day_gainers&count=25",
    "most_active":  "https://query1.finance.yahoo.com/v1/finance/screener/predefined/saved?formatted=false&scrIds=most_active&count=25",
}

HEADERS = {"User-Agent": "Mozilla/5.0"}

# ── Helpers ────────────────────────────────────────────────────────────────

def load_config() -> dict:
    try:
        with open(CONFIG_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Could not load config.json: {e}")
        return {}

def load_tickers() -> list:
    try:
        with open(TICKERS_FILE, "r") as f:
            data = json.load(f)
        return data.get("combined_list", [])
    except Exception:
        return []

def save_tickers(tickers: list):
    try:
        with open(TICKERS_FILE, "r") as f:
            data = json.load(f)
        data["combined_list"] = list(dict.fromkeys(tickers))  # deduplicate
        with open(TICKERS_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception as e:
        logger.error(f"Could not save tickers: {e}")

# ── Live SRS Helper ────────────────────────────────────────────────────────

def get_spy_return_today() -> float:
    """Fetches today's intraday SPY return as a simple proxy for market strength."""
    try:
        spy = yf.Ticker("SPY").history(period="2d", interval="1d", auto_adjust=True)
        if len(spy) < 2:
            return 0.0
        ret = float(np.log(spy["Close"].iloc[-1] / spy["Close"].iloc[-2]))
        return ret
    except Exception:
        return 0.0

def get_ticker_return_today(symbol: str) -> float:
    """Fetches today's intraday return for a single ticker."""
    try:
        df = yf.Ticker(symbol).history(period="2d", interval="1d", auto_adjust=True)
        if len(df) < 2:
            return 0.0
        return float(np.log(df["Close"].iloc[-1] / df["Close"].iloc[-2]))
    except Exception:
        return 0.0

# ── AI-based scoring ───────────────────────────────────────────────────────

def score_ticker(symbol: str, spy_ret: float, config: dict) -> float:
    """
    Returns AI win probability for the ticker, or 0.0 if it cannot be scored.
    Applies a quick Sector-Relative Strength pre-filter: if the ticker is
    already underperforming SPY today, we skip expensive model inference.
    """
    ticker_ret = get_ticker_return_today(symbol)
    srs = ticker_ret - spy_ret

    if srs < SRS_THRESHOLD:
        logger.info(f"[Discovery] {symbol} SRS {srs:+.4f} — below threshold. Skipping.")
        return 0.0

    # Lazy-import quant engine to avoid heavy cost at module load
    try:
        from quant_inference import QuantInference
        from strategy import Strategy

        model_path = config.get("ml_model_path", "trained_models/ai_brain_v1.pkl")
        qe = QuantInference(model_path)
        if not qe.is_ai_active():
            logger.warning("[Discovery] AI model not loaded. Returning SRS score only.")
            return 0.55 if srs > SRS_THRESHOLD else 0.0

        strat = Strategy()
        result = strat.analyze(symbol, quant_engine=qe)
        prob = result.get("ai_win_prob", 0.50)
        logger.info(f"[Discovery] {symbol} SRS={srs:+.4f} | AI Prob={prob:.2%}")
        return prob

    except Exception as e:
        logger.error(f"[Discovery] Could not score {symbol}: {e}")
        return 0.0

# ── Screener + Processing ──────────────────────────────────────────────────

def fetch_screener(url: str) -> list:
    """Returns a list of Yahoo Finance quote dicts from the screener URL."""
    try:
        resp = requests.get(url, headers=HEADERS, timeout=10)
        if resp.status_code == 200:
            return resp.json().get("finance", {}).get("result", [{}])[0].get("quotes", [])
    except Exception as e:
        logger.error(f"[Discovery] Screener fetch failed: {e}")
    return []

def run_discovery_cycle():
    logger.info("═══════════════════════════════════════")
    logger.info("  AI Auto-Discovery — Starting Scan")
    logger.info("═══════════════════════════════════════")

    config       = load_config()
    current_list = load_tickers()
    spy_ret      = get_spy_return_today()
    logger.info(f"[Discovery] SPY today: {spy_ret:+.4f}")

    discovered_symbols: list[str] = []

    for name, url in SCRENER_URLS.items():
        logger.info(f"[Discovery] Scanning screener: {name}")
        quotes = fetch_screener(url)
        for q in quotes:
            symbol = q.get("symbol", "")
            if not symbol or "_" in symbol:
                # Skip already-in-T212-format or weird symbols
                continue

            # Build T212 ticker format guess
            t212 = f"{symbol}_US_EQ"

            if t212 in current_list:
                logger.info(f"[Discovery] {symbol} already in watchlist. Skipping.")
                continue

            prob = score_ticker(symbol, spy_ret, config)
            if prob >= AI_WIN_THRESHOLD:
                logger.info(f"[Discovery] ✅ {symbol} PASSED (AI: {prob:.2%}) — Adding to watchlist for {DISCOVERY_TTL_HRS}h!")
                discovered_symbols.append(t212)
            else:
                logger.info(f"[Discovery] ❌ {symbol} FAILED (AI: {prob:.2%}) — Skipping.")

    if discovered_symbols:
        new_list = list(dict.fromkeys(current_list + discovered_symbols))
        save_tickers(new_list)
        logger.info(f"[Discovery] Added {len(discovered_symbols)} new ticker(s): {discovered_symbols}")
    else:
        logger.info("[Discovery] No new high-confidence tickers found this cycle.")

    logger.info("[Discovery] Scan complete.")


if __name__ == "__main__":
    # Run once, then sleep 1 hour, repeat all day
    while True:
        run_discovery_cycle()
        logger.info("[Discovery] Sleeping 1 hour before next scan...")
        time.sleep(3600)
