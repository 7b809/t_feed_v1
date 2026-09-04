@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Option Feed Engine
echo ================================================
echo.

set "VENV_PATH="

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
)

echo.
echo Python:
where python
python --version
echo.

echo ================================================
echo Starting Option Feed Engine with Uvicorn...
echo ================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000

pause