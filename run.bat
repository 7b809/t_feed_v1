@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Option Feed Engine
echo ================================================
echo.

REM ==================================================
REM Check Virtual Environment
REM ==================================================

if exist "myenv" (
    echo Virtual environment found.
    echo Assuming required packages are already installed.
    echo.
) else (
    echo Virtual environment not found.
    echo Creating virtual environment...
    echo.

    python -m venv myenv

    if errorlevel 1 (
        echo ERROR: Failed to create virtual environment.
        pause
        exit /b 1
    )

    echo.
    echo Activating virtual environment...
    call "myenv\Scripts\activate.bat"

    echo.
    echo Upgrading pip...
    python -m pip install --upgrade pip

    if errorlevel 1 (
        echo ERROR: Failed to upgrade pip.
        pause
        exit /b 1
    )

    if exist "requirements.txt" (
        echo.
        echo Installing required packages...
        python -m pip install -r requirements.txt

        if errorlevel 1 (
            echo ERROR: Failed to install required packages.
            pause
            exit /b 1
        )
    )

    echo.
    echo Environment setup completed.
    echo.
)

REM ==================================================
REM Activate Virtual Environment
REM ==================================================

echo Activating virtual environment...
call "myenv\Scripts\activate.bat"

if errorlevel 1 (
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
REM Start FastAPI / Uvicorn
REM ==================================================

echo ================================================
echo Starting Option Feed Engine with Uvicorn...
echo ================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause