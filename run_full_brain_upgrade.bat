@echo off
TITLE TRDBT - AI Brain Upgrade Pipeline
COLOR 0A

echo =======================================================
echo    TRDBT: PHASE 4 ULTIMATE BRAIN UPGRADE
echo =======================================================
echo.

:: 1. Sync Latest Code (Crucial to get AI architecture updates from Antigravity)
echo [STEP 1/4] Checking for latest Phase 4 logic from GitHub...
git pull origin main --quiet
if %ERRORLEVEL% NEQ 0 (
    echo [WARNING] Could not pull latest code. Continuing with local version...
)

:: 2. Data Ingestion (Macro + Micro + Benchmarks)
echo.
echo [STEP 2/4] Running Data Lake Ingestion (Downloading SPY, QQQ, IWM + Tickers)...
python ai_data_lake/data_ingestion.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Data Ingestion failed.
    pause
    exit /b
)

:: 3. Wipe stale processed files BEFORE Feature Engineering
::    Without this, old broken files mix with new ones and wipe all rows in the trainer.
echo.
:: 3. Wipe stale processed files BEFORE Feature Engineering
::    Without this, old broken files mix with new ones and wipe all rows in the trainer.
echo.
echo [STEP 3a/4] Clearing stale processed_data files...
python -c "from pathlib import Path; import json; try: d = Path(json.load(open('node_config.json')).get('shared_drive_path', r'D:\trd-data')) / 'processed_data'; [f.unlink() for f in d.glob('*.parquet')] if d.exists() else None; print(f'Cleared stale files from {d}') except Exception as e: print(f'Skip clear: {e}')"

:: 4. Feature Engineering (The Stitching Engine)
echo.
echo [STEP 3/4] Running Feature Engineering (Stitching MTF + Relative Strength)...
python ai_data_lake/feature_engineering.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Feature Engineering failed.
    pause
    exit /b
)

:: 4. Deep Trainer (GPU-Native Learning)
echo.
echo [STEP 4/4] Starting Deep Trainer (3080 Ti Mode)...
echo [INFO] Looking for approx 0.13 LogLoss or better.
python ai_deep_trainer/model_training.py
if %ERRORLEVEL% NEQ 0 (
    echo [ERROR] Deep Trainer failed.
    pause
    exit /b
)

echo.
echo =======================================================
echo    BRAIN UPGRADE COMPLETE! 🦾🔥
echo.
echo    Next Steps:
echo    1. Verify the ai_brain_v1.pkl looks fresh.
echo    2. Commit and push the .pkl to GitHub manually.
echo    3. Restart your bot on the LXC to load the new brain.
echo =======================================================
pause
