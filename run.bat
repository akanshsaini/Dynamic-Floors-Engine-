@echo off
title Ad Floor Suggestions - ML Powered
echo.
echo ============================================================
echo   Ad Floor Suggestions - ML Powered
echo ============================================================
echo.

cd /d "%~dp0"

REM Check Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [ERROR] Python is not installed or not in PATH.
    echo Please install Python from https://python.org
    pause
    exit /b 1
)

REM Create venv if needed
if not exist "venv" (
    echo Creating virtual environment...
    python -m venv venv
    call venv\Scripts\activate.bat
    echo Installing dependencies...
    pip install -r requirements.txt --quiet
) else (
    call venv\Scripts\activate.bat
    REM Quick check core dependencies are installed
    python -c "import flask, pandas, numpy, dotenv" >nul 2>&1
    if %errorlevel% neq 0 (
        echo Installing missing dependencies...
        pip install -r requirements.txt --quiet
    )
)

REM Create required directories
if not exist "uploads" mkdir uploads
if not exist "outputs" mkdir outputs

echo.
echo Starting server at http://localhost:5000
echo Press Ctrl+C to stop.
echo.

REM Open browser
start "" "http://localhost:5000"

REM Start Flask
python app.py

pause
