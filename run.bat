@echo off
cls

echo Activating virtual environment...
call myenv\Scripts\activate

echo.
echo Starting Option Feed Engine with Uvicorn...
echo.

python -m uvicorn main:app --host 0.0.0.0 --port 8000 --reload

pause