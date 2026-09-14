@echo off
cls

echo ==========================================
echo     Upstox Order Request Receiver
echo ==========================================
echo.

REM ------------------------------------------
REM Check whether virtual environment exists
REM ------------------------------------------
if exist "myenv\Scripts\activate.bat" (
    echo [OK] myenv environment already exists.
    echo.
) else (
    echo [INFO] myenv environment not found.
    echo [INFO] Creating virtual environment...
    echo.

    python -m venv myenv

    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to create virtual environment.
        pause
        exit /b 1
    )

    echo.
    echo [OK] Virtual environment created.
    echo.
)

REM ------------------------------------------
REM Activate virtual environment
REM ------------------------------------------
echo [INFO] Activating myenv...
call myenv\Scripts\activate.bat

if errorlevel 1 (
    echo.
    echo [ERROR] Failed to activate virtual environment.
    pause
    exit /b 1
)

echo [OK] Virtual environment activated.
echo.

REM ------------------------------------------
REM Install packages if requirements.txt exists
REM ------------------------------------------
if exist "requirements.txt" (
    echo [INFO] Installing/checking required packages...
    echo.

    python -m pip install --upgrade pip
    python -m pip install -r requirements.txt

    if errorlevel 1 (
        echo.
        echo [ERROR] Failed to install required packages.
        pause
        exit /b 1
    )

    echo.
    echo [OK] Required packages are ready.
    echo.
) else (
    echo [WARNING] requirements.txt not found.
    echo [INFO] Skipping package installation.
    echo.
)

REM ------------------------------------------
REM Start FastAPI application
REM ------------------------------------------
echo ==========================================
echo     Starting FastAPI application...
echo ==========================================
echo.
echo Server: http://localhost:8001
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8001

REM ------------------------------------------
REM Keep window open after server stops
REM ------------------------------------------
echo.
echo ==========================================
echo     FastAPI application stopped
echo ==========================================
echo.

pause