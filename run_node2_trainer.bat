@echo off
SETLOCAL
echo ============================================================
echo   Node 2: Deep Trainer - Continuous GPU Learning (Windows)
echo ============================================================

echo Starting model_training.py...
echo (Note: This script has its own internal 12-hour sleep loop)

python ai_deep_trainer\model_training.py

echo Script stopped unexpectedly.
pause
