@echo off
cls
cd /d "%~dp0"

echo ================================================
echo      Option Feed Engine
echo ================================================
echo.


REM ============================================================
REM CLEAN RUNTIME DIRECTORIES
REM ============================================================
REM
REM IMPORTANT:
REM Do NOT delete the entire "data" directory.
REM
REM The application stores option snapshots inside:
REM
REM     data\readonly
REM     data\runtime
REM
REM
REM This cleanup removes only folders that are safe to
REM recreate on every startup.
REM
REM ============================================================

echo ================================================
echo Cleaning runtime directories...
echo ================================================
echo.


REM ------------------------------------------------------------
REM Logs
REM ------------------------------------------------------------

if exist "logs" (
    echo Removing: logs
    rmdir /s /q "logs"

    if exist "logs" (
        echo.
        echo ERROR: Could not completely remove logs.
        echo A process may still be using a log file.
        echo.
        pause
        exit /b 1
    )
)

mkdir "logs"


REM ------------------------------------------------------------
REM Output
REM ------------------------------------------------------------

if exist "output" (
    echo Removing: output
    rmdir /s /q "output"

    if exist "output" (
        echo.
        echo ERROR: Could not completely remove output.
        echo A process may still be using a file.
        echo.
        pause
        exit /b 1
    )
)

mkdir "output"


REM ------------------------------------------------------------
REM Temporary directory
REM ------------------------------------------------------------

if exist "temp" (
    echo Removing: temp
    rmdir /s /q "temp"

    if exist "temp" (
        echo.
        echo ERROR: Could not completely remove temp.
        echo A process may still be using a file.
        echo.
        pause
        exit /b 1
    )
)

mkdir "temp"


echo.
echo Runtime directories cleaned successfully.
echo.


REM ============================================================
REM VIRTUAL ENVIRONMENT
REM ============================================================

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


REM ============================================================
REM ACTIVATE VIRTUAL ENVIRONMENT
REM ============================================================

echo Activating virtual environment...

call "%VENV_PATH%\Scripts\activate.bat"

if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)


REM ============================================================
REM PYTHON INFORMATION
REM ============================================================

echo.
echo Python:
where python
python --version
echo.


REM ============================================================
REM START APPLICATION
REM ============================================================

echo ================================================
echo Starting Option Feed Engine with Uvicorn...
echo ================================================
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000


REM ============================================================
REM APPLICATION EXIT
REM ============================================================

echo.
echo ================================================
echo Option Feed Engine stopped.
echo ================================================
echo.

pause
