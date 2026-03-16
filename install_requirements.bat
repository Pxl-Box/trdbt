@echo off
SETLOCAL
echo ============================================================
echo   Installing ML Architecture Dependencies (Windows)
echo ============================================================

REM Check if Python is installed
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python not found. Please install Python 3.10+ and add it to your PATH.
    pause
    exit /b
)

echo [1/2] Installing core data science stack...
pip install pandas numpy yfinance pyarrow fastparquet scikit-learn

echo [2/2] Installing GPU-accelerated XGBoost...
pip install xgboost

echo ============================================================
echo   Installation Complete!
echo ============================================================
pause
