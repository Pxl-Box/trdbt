#!/bin/bash
# ============================================================
# Node 1: Data Lake - Continuous Runner
# Run this script on your 24/7 30TB server.
# It will continuously refresh market data and re-engineer features.
# ============================================================

echo "=== Starting Node 1: Data Lake & Feature Engineering ==="

while true; do
    echo "[$(date)] Waking up to refresh Data Lake..."
    
    echo "--- Step 1/2: Downloading fresh market data from Yahoo Finance ---"
    python3 data_ingestion.py
    
    echo "--- Step 2/2: Re-computing ML features and saving .parquet files ---"
    python3 feature_engineering.py
    
    echo "[$(date)] Cycle complete. Sleeping for 6 hours..."
    sleep 21600  # 6 hours
done
