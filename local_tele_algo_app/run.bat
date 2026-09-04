@echo off
setlocal

cd /d "%~dp0\.."

set "LOCAL_ALERT_APP_HOST=127.0.0.1"
set "LOCAL_ALERT_APP_PORT=9000"

echo ================================================
echo Local Telegram and Algo App Simulator
echo ================================================
echo Dashboard: http://127.0.0.1:9000/
echo API Docs : http://127.0.0.1:9000/docs
echo Health   : http://127.0.0.1:9000/health
echo ================================================
echo.

python -m uvicorn local_tele_algo_app.local_main:app --host 127.0.0.1 --port 9000 --reload

if errorlevel 1 (
    echo.
    echo Local alert simulator failed to start.
    pause
    exit /b 1
)

endlocal