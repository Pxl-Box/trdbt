@echo off
SETLOCAL
echo ============================================================
echo   Node 1: Data Lake - Continuous Runner (Windows)
echo ============================================================

:loop
echo [%DATE% %TIME%] Waking up to refresh Data Lake...

echo --- Step 1/2: Downloading fresh market data ---
python ai_data_lake\data_ingestion.py

echo --- Step 2/2: Re-computing ML features ---
python ai_data_lake\feature_engineering.py

echo [%DATE% %TIME%] Cycle complete. Waiting 6 hours...
timeout /t 21600 /nobreak
goto loop
