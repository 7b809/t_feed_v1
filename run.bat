cls
@echo off

echo ==========================================
echo   Upstox Order Request Receiver
echo ==========================================
echo.

echo Starting FastAPI application...
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8001

pause