@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Market Data Foundation
echo ================================================
echo.

set "VENV_PATH="
set DIRS_TO_CLEAR=data logs logs_text
set DIRS_DISPLAY=data, logs, logs_text

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

echo ================================================
echo Cleanup directories cleared: %DIRS_DISPLAY%
echo ================================================
echo.

python -m uvicorn app.main:app --host 0.0.0.0 --port 8004

