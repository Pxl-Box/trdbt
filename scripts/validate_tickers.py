import json
import yfinance as yf
import pandas as pd
import sys
from pathlib import Path

# Add project root to path
sys.path.append(str(Path(__file__).parent.parent))
from strategy import MeanReversionStrategy

def validate_tickers():
    tickers_file = "trdbt_tickers.json"
    if not Path(tickers_file).exists():
        print(f"Error: {tickers_file} not found.")
        return

    with open(tickers_file, "r") as f:
        data = json.load(f)
        tickers = data.get("combined_list", [])

    print(f"Validating {len(tickers)} tickers from {tickers_file}...\n")
    
    strategy = MeanReversionStrategy()
    results = []
    
    for ticker in tickers:
        print(f"Checking {ticker}...", end=" ", flush=True)
        try:
            df = strategy.get_historical_data(ticker, period="5d", interval="1h")
            if df.empty:
                print("❌ FAILED (Empty data)")
                results.append({"ticker": ticker, "status": "FAIL", "reason": "Empty Data"})
            else:
                print(f"✅ OK ({len(df)} rows)")
                results.append({"ticker": ticker, "status": "OK", "rows": len(df)})
        except Exception as e:
            print(f"❌ ERROR: {e}")
            results.append({"ticker": ticker, "status": "ERROR", "reason": str(e)})

    # Summary
    print("\n" + "="*30)
    print("VALIDATION SUMMARY")
    print("="*30)
    ok_count = len([r for r in results if r["status"] == "OK"])
    fail_count = len([r for r in results if r["status"] != "OK"])
    print(f"Total: {len(tickers)}")
    print(f"Healthy: {ok_count}")
    print(f"Broken: {fail_count}")
    
    if fail_count > 0:
        print("\nBroken Tickers:")
        for r in results:
            if r["status"] != "OK":
                print(f"- {r['ticker']}: {r.get('reason', 'Unknown')}")

if __name__ == "__main__":
    validate_tickers()
