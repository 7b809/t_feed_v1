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

if exist "requirements.txt" (
    echo ================================================
    echo Installing required packages...
    echo ================================================
    echo.

    python -m pip install --upgrade pip

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to upgrade pip.
        pause
        exit /b 1
    )

    echo.
    echo Installing requirements.txt...
    echo.

    python -m pip install -r requirements.txt

    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install required packages.
        pause
        exit /b 1
    )

    echo.
    echo Required packages installed successfully.
    echo.
) else (
    echo WARNING: requirements.txt not found.
    echo Skipping package installation.
    echo.
)

REM ==================================================
REM Start FastAPI / Uvicorn
REM ==================================================

echo ================================================
echo Starting Option Feed Engine with Uvicorn...
echo ================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause