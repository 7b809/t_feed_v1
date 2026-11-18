@echo off
setlocal EnableExtensions EnableDelayedExpansion
cd /d "%~dp0"

set "ENV_FILE=.env"
set "VENV_DIR=myenv"
set "REQUIREMENTS_FILE=requirements.txt"
set "DIRS_TO_CLEAR=logs logs_text"
set "DIRS_DISPLAY=logs, logs_text"

echo ================================================
echo             Upstox Order Service
echo ================================================
echo.

rem --------------------------------------------------
rem Resolve configuration (shell env > .env > default)
rem --------------------------------------------------
rem Port precedence (matches core/config.py and start.sh):
rem   shell API_PORT > .env API_PORT > shell APP_PORT > .env APP_PORT > 8004
rem APP_PORT is kept as a backward-compatible alias.

if not defined API_PORT call :read_env API_PORT
if not defined API_PORT if defined APP_PORT set "API_PORT=!APP_PORT!"
if not defined API_PORT call :read_env APP_PORT
if not defined API_PORT if defined APP_PORT set "API_PORT=!APP_PORT!"
if not defined API_PORT set "API_PORT=8004"
set "PORT=!API_PORT!"

rem IP binding (shell env > .env > default)
if not defined ENABLE_IP_BINDING call :read_env ENABLE_IP_BINDING
if not defined ENABLE_IP_BINDING set "ENABLE_IP_BINDING=false"
if not defined BIND_IP call :read_env BIND_IP
if not defined BIND_IP set "BIND_IP=127.0.0.1"

rem --------------------------------------------------
rem Resolve bind host based on feature flag
rem --------------------------------------------------

if /I not "!ENABLE_IP_BINDING!"=="true" goto :bind_default

rem --- IP binding ENABLED ---
set "HOST=!BIND_IP!"

echo !BIND_IP!| findstr /r "^[0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*[.][0-9][0-9]*$" >nul
if errorlevel 1 (
    echo ERROR: Invalid BIND_IP '!BIND_IP!'. Expected IPv4 like 192.168.1.50.
    exit /b 1
)

call :check_local_interface
if not defined FOUND (
    echo WARNING: !BIND_IP! is not configured on any local interface.
    echo Uvicorn may fail to bind. Continuing anyway...
    echo.
)

echo IP binding ENABLED. Binding to !HOST!
goto :bind_done

rem --- IP binding DISABLED (default) ---
:bind_default
set "HOST=0.0.0.0"
echo IP binding DISABLED. Binding to !HOST! (all interfaces^)

:bind_done
echo.

rem --------------------------------------------------
rem Virtual environment
rem --------------------------------------------------

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
echo !VENV_PATH!
echo.

echo Activating virtual environment...
call "!VENV_PATH!\Scripts\activate.bat"
if errorlevel 1 (
    echo.
    echo ERROR: Failed to activate virtual environment.
    pause
    exit /b 1
)
echo Virtual environment activated successfully.
echo.

rem --------------------------------------------------
rem Dependencies
rem --------------------------------------------------

if exist "%REQUIREMENTS_FILE%" (
    echo Checking Python dependencies...
    python -m pip install -r "%REQUIREMENTS_FILE%"
    if errorlevel 1 (
        echo.
        echo ERROR: Failed to install Python packages.
        pause
        exit /b 1
    )
    echo.
    echo Python dependencies are ready.
) else (
    echo WARNING: %REQUIREMENTS_FILE% not found. Skipping dependency installation.
)
echo.

echo Checking required Python packages...
python -c "import upstox_client; print('upstox_client: OK')"
if errorlevel 1 (
    echo.
    echo ERROR: upstox_client is not installed in the virtual environment.
    echo Install it using:
    echo   python -m pip install upstox-python-sdk
    pause
    exit /b 1
)
echo.

rem --------------------------------------------------
rem Cleanup
rem --------------------------------------------------

cls
echo ================================================
echo Starting Upstox Order Service with Uvicorn...
echo ================================================
echo.

for %%d in (%DIRS_TO_CLEAR%) do (
    if exist "%%d" rmdir /s /q "%%d"
)

if not exist "data" mkdir "data"

echo ================================================
echo Cleanup directories cleared: %DIRS_DISPLAY%
echo ================================================
echo.

rem --------------------------------------------------
rem Start FastAPI
rem --------------------------------------------------

echo Starting FastAPI...
echo Host: !HOST!
echo Port: !PORT!
echo.

python -m uvicorn main:app --host !HOST! --port !PORT!

goto :eof

rem --------------------------------------------------
rem Helpers
rem --------------------------------------------------
:read_env
rem Reads KEY=value from .env. Assumes standard format with no spaces
rem around '='. Surrounding quotes are stripped. Existing env vars
rem are NOT overridden by the caller (caller guards with 'if not defined').
if not exist "%ENV_FILE%" goto :eof
for /f "usebackq tokens=1,* delims==" %%A in ("%ENV_FILE%") do (
    if /I "%%A"=="%~1" set "%~1=%%~B"
)
goto :eof

:check_local_interface
rem Sets FOUND=1 when BIND_IP appears on a local interface, else leaves FOUND unset.
set "FOUND="
for /f "tokens=2 delims=:" %%A in ('ipconfig 2^>nul ^| findstr /i "IPv4"') do (
    set "IP=%%A"
    set "IP=!IP: =!"
    if "!IP!"=="!BIND_IP!" set "FOUND=1"
)
goto :eof