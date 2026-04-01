import yfinance as yf
import pandas as pd

tickers = ["PARA", "NOVA", "VUSA_US_EQ", "META_US_EQ"]

for t in tickers:
    print(f"Testing {t}...")
    try:
        # bot.py uses ticker.split("_")[0]
        yf_ticker = t.split("_")[0]
        df = yf.download(yf_ticker, period="10d", interval="15m", progress=False)
        if df.empty:
            print(f"  FAILED: Dataframe is empty for {yf_ticker}")
        else:
            print(f"  SUCCESS: Downloaded {len(df)} rows for {yf_ticker}")
    except Exception as e:
        print(f"  ERROR for {t}: {e}")
