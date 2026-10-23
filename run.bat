:: ============================================================
:: File: run.bat
:: Path: run.bat
:: Purpose: Pure-Batch bootstrap & launcher for mega_archive_service.
::          - cls
::          - verify python
::          - create myenv ONLY if missing
::          - activate myenv
::          - upgrade pip
::          - pip install -r requirements.txt  (auto-repair drift)
::          - verify 'mega_sdk' importable (install if missing)
::          - check .env present
::          - launch uvicorn on 0.0.0.0:8002
:: Usage:   double-click this file, or run "run.bat" from cmd.
:: ============================================================

@echo off
setlocal EnableExtensions
cls

:: ------------------------------------------------------------
:: Move to script directory (no matter where it was launched)
:: ------------------------------------------------------------
pushd "%~dp0"

set "VENV_DIR=%~dp0myenv"
set "VENV_PY=%VENV_DIR%\Scripts\python.exe"
set "VENV_ACT=%VENV_DIR%\Scripts\activate.bat"
set "REQ_FILE=%~dp0requirements.txt"
set "ENV_FILE=%~dp0.env"
set "HOST=0.0.0.0"
set "PORT=8002"

:: ------------------------------------------------------------
:: Banner
:: ------------------------------------------------------------
echo ==================================================
echo   mega_archive_service  ::  bootstrap ^& run
echo   Dir : %~dp0
echo   Bind: http://%HOST%:%PORT%
echo ==================================================
echo.

:: ------------------------------------------------------------
:: 1. Python present?
:: ------------------------------------------------------------
where python >nul 2>&1
if errorlevel 1 (
    echo [XX] 'python' not found on PATH. Install Python 3.11+ and re-run.
    goto :fail
)
for /f "tokens=*" %%v in ('python --version 2^>^&1') do set "PYVER=%%v"
echo [OK] Found %PYVER%

:: ------------------------------------------------------------
:: 2. Virtual env exists? (create only if missing)
:: ------------------------------------------------------------
if exist "%VENV_PY%" (
    echo [OK] Virtual env present: %VENV_DIR%
    goto :activate
)

echo [..] Virtual env missing - creating %VENV_DIR% ...
python -m venv "%VENV_DIR%"
if errorlevel 1 (
    echo [XX] Failed to create virtual env.
    goto :fail
)
if not exist "%VENV_PY%" (
    echo [XX] venv creation returned 0 but python.exe is missing.
    goto :fail
)
echo [OK] Virtual env created.

:activate
:: ------------------------------------------------------------
:: 3. Activate virtual env
:: ------------------------------------------------------------
echo [..] Activating virtual env ...
call "%VENV_ACT%"
if errorlevel 1 (
    echo [XX] Failed to activate virtual env.
    goto :fail
)
echo [OK] Activated.

:: ------------------------------------------------------------
:: 4. Upgrade pip (quiet, safe every run)
:: ------------------------------------------------------------
echo [..] Ensuring pip is up-to-date ...
python -m pip install --upgrade pip --quiet --disable-pip-version-check
echo [OK] pip ready.

:: ------------------------------------------------------------
:: 5. Install / sync requirements.txt
:: ------------------------------------------------------------
if exist "%REQ_FILE%" (
    echo [..] Syncing dependencies from requirements.txt ...
    python -m pip install -r "%REQ_FILE%" --quiet --disable-pip-version-check
    if errorlevel 1 (
        echo [XX] pip install -r requirements.txt failed. See output above.
        goto :fail
    )
    echo [OK] Dependencies in sync.
) else (
    echo [!!] requirements.txt not found - skipping bulk install.
)

:: ------------------------------------------------------------
:: 6. Guarantee mega_sdk is importable (fixes previous bug)
:: ------------------------------------------------------------
python -c "import mega_sdk" >nul 2>&1
if errorlevel 1 (
    echo [!!] mega_sdk not importable - installing 'mega-sdk' ...
    python -m pip install mega-sdk --quiet --disable-pip-version-check
    python -c "import mega_sdk" >nul 2>&1
    if errorlevel 1 (
        echo [XX] mega_sdk STILL not importable after install. Aborting.
        goto :fail
    )
)
echo [OK] mega_sdk import OK.

:: ------------------------------------------------------------
:: 7. .env sanity check
:: ------------------------------------------------------------
if not exist "%ENV_FILE%" (
    echo [!!] .env not found at %ENV_FILE% - app may fail on missing config.
) else (
    echo [OK] .env found.
)

:: ------------------------------------------------------------
:: 8. Launch uvicorn
:: ------------------------------------------------------------
echo.
echo ==================================================
echo   Starting uvicorn -^> http://%HOST%:%PORT%
echo   Press CTRL+C to stop
echo ==================================================
echo.

python -m uvicorn app.main:app --host %HOST% --port %PORT%
set "RC=%ERRORLEVEL%"

echo.
if "%RC%"=="0" (
    echo [OK] Service stopped cleanly.
) else (
    echo [XX] Service exited with code %RC%.
)

popd
echo.
pause
endlocal
exit /b %RC%

:: ------------------------------------------------------------
:: Failure exit path
:: ------------------------------------------------------------
:fail
popd
echo.
pause
endlocal
exit /b 1