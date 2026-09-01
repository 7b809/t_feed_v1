@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Option Feed Engine
echo ================================================
echo.

REM ==================================================
REM Check/Create Virtual Environment
REM ==================================================

if not exist "myenv\Scripts\python.exe" (
    echo Virtual environment not found.
    echo.
    echo Creating virtual environment...
    echo.

    python -m venv myenv

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to create virtual environment.
        echo Make sure Python is installed and available in PATH.
        pause
        exit /b 1
    )

    echo.
    echo Virtual environment created successfully.
    echo.
) else (
    echo Virtual environment already exists.
    echo.
)

REM ==================================================
REM Activate Virtual Environment
REM ==================================================

echo Activating virtual environment...
call "myenv\Scripts\activate.bat"

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

REM ==================================================
REM Install/Update Required Packages
REM ==================================================


REM ==================================================
REM Start FastAPI / Uvicorn
REM ==================================================

echo ================================================
echo Starting Option Feed Engine with Uvicorn...
echo ================================================
echo.

@REM python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

python -m uvicorn main:app --host 0.0.0.0 --port 8000 

pause