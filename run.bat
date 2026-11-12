@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Market Data Foundation
echo ================================================
echo.

set "VENV_PATH="
set DIRS_TO_CLEAR=logs logs_text
set DIRS_DISPLAY=logs, logs_text

if exist "myenv\Scripts\python.exe" (
    set "VENV_PATH=%CD%\myenv"
    echo Virtual environment found in current folder.
) else (
    if exist "..\myenv\Scripts\python.exe" (
        set "VENV_PATH=%CD%\..\myenv"
        echo Virtual environment found in parent folder.
    ) else (
        echo Virtual environment not found.
        echo Creating virtual environment in current folder...
        echo.

        python -m venv myenv

        if errorlevel 1 (
            echo.
            echo ERROR: Failed to create virtual environment.
            echo Make sure Python is installed and available in PATH.
            pause
            exit /b 1
        )

        set "VENV_PATH=%CD%\myenv"
        echo.
        echo Virtual environment created successfully.
    )
)

echo.
echo Using virtual environment:
echo %VENV_PATH%
echo.

echo Activating virtual environment...
call "%VENV_PATH%\Scripts\activate.bat"


if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
) else (
    echo Virtual environment activated successfully.
    pip install -r requirements.txt
    echo requirements installed successfully.
    echo.
)

cls

echo ================================================
echo Starting isolation_consumer_service with Uvicorn...
echo ================================================
echo.



for %%d in (%DIRS_TO_CLEAR%) do (
    if exist "%%d" rmdir /s /q "%%d"
)

rem Preserve data/live_feed because it contains the requested raw market history.
rem Rebuild today's indicator state and isolation mirror on startup.
if exist "data\sr_state" rmdir /s /q "data\sr_state"
if exist "data\supertrend_state" rmdir /s /q "data\supertrend_state"
if exist "data\instrument_isolation.json" del /q "data\instrument_isolation.json"
if not exist "data" mkdir "data"

echo ================================================
echo Cleanup directories cleared: %DIRS_DISPLAY%
echo SR and Supertrend state cleared; data\live_feed preserved.
echo ================================================
echo.

if not defined APP_PORT set "APP_PORT=8003"
python -m uvicorn app.main:app --host 0.0.0.0 --port %APP_PORT%

