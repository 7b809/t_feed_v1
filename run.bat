@echo off
setlocal
cd /d "%~dp0"

echo ================================================
echo             Upstox Order Service
echo ================================================
echo.

set "PYTHON_CMD=python"
if exist "myenv\Scripts\python.exe" (
    set "PYTHON_CMD=myenv\Scripts\python.exe"
) else (
    echo Creating virtual environment...
    python -m venv myenv
    if errorlevel 1 (
        echo ERROR: Could not create virtual environment.
        pause
        exit /b 1
    )
    set "PYTHON_CMD=myenv\Scripts\python.exe"
)

"%PYTHON_CMD%" -m pip install --upgrade pip
if errorlevel 1 goto :error
"%PYTHON_CMD%" -m pip install -r requirements.txt
if errorlevel 1 goto :error

if not exist "data\orders" mkdir "data\orders"
if not exist "logs" mkdir "logs"
if not exist "logs_text" mkdir "logs_text"

for /f "delims=" %%H in ('"%PYTHON_CMD%" -c "from core.config import API_HOST; print(API_HOST)"') do set "APP_HOST=%%H"
for /f "delims=" %%P in ('"%PYTHON_CMD%" -c "from core.config import API_PORT; print(API_PORT)"') do set "APP_PORT=%%P"

echo Starting on %APP_HOST%:%APP_PORT%
echo Note: this launcher does NOT clear data, logs, or order history.
"%PYTHON_CMD%" -m uvicorn main:app --host %APP_HOST% --port %APP_PORT%
goto :eof

:error
echo ERROR: Dependency installation failed.
pause
exit /b 1
