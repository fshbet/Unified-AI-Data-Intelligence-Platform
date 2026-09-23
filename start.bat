@echo off
setlocal EnableDelayedExpansion
title Enterprise Data Intelligence - launcher
cd /d "%~dp0"

echo ==========================================================
echo   Enterprise Data Intelligence - starting the application
echo ==========================================================
echo.

REM ---------------------------------------------------------------- prereqs
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python 3.11+ was not found on PATH.
    echo         Install it from https://python.org and run this file again.
    pause & exit /b 1
)
where npm >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Node.js / npm was not found on PATH.
    echo         Install Node 20+ from https://nodejs.org and run this file again.
    pause & exit /b 1
)

set PYTHONIOENCODING=utf-8

REM ------------------------------------------------------- backend env setup
if not exist ".venv\Scripts\python.exe" (
    echo [setup] Creating the Python virtual environment ...
    python -m venv .venv
    if errorlevel 1 ( echo [ERROR] Could not create the virtual environment. & pause & exit /b 1 )
    echo [setup] Installing backend dependencies - first run only, this takes a few minutes ...
    ".venv\Scripts\python.exe" -m pip install -q -r backend\requirements.txt "pydantic[email]"
    if errorlevel 1 ( echo [ERROR] Installing backend dependencies failed. & pause & exit /b 1 )
)

REM ------------------------------------------------------ frontend env setup
if not exist "frontend\node_modules" (
    echo [setup] Installing frontend dependencies - first run only ...
    pushd frontend
    call npm install --no-audit --no-fund
    if errorlevel 1 ( popd & echo [ERROR] npm install failed. & pause & exit /b 1 )
    popd
)

REM ------------------------------------- demo data + catalog, first run only
if not exist "data\samples\sales.csv" (
    echo [setup] Generating the synthetic enterprise dataset ...
    ".venv\Scripts\python.exe" scripts\generate_demo_data.py
    if errorlevel 1 ( echo [ERROR] Generating the demo dataset failed. & pause & exit /b 1 )
)
if not exist "data\metadata.db" (
    echo [setup] Seeding the catalog - sources, metrics, glossary, relationships, insights ...
    ".venv\Scripts\python.exe" scripts\seed_demo.py
    if errorlevel 1 ( echo [ERROR] Seeding the catalog failed. & pause & exit /b 1 )
)

REM ------------------------------- pick the first free backend port and wire
REM                                 the frontend to it before anything starts
set "BACKEND_PORT="
for %%P in (8000 8010 8020 8030 8040) do (
    if not defined BACKEND_PORT (
        netstat -ano -p tcp | findstr /C:":%%P " | findstr /C:"LISTENING" >nul 2>&1
        if errorlevel 1 set "BACKEND_PORT=%%P"
    )
)
if not defined BACKEND_PORT (
    echo [ERROR] Ports 8000-8040 are all in use. Free one and run this file again.
    pause & exit /b 1
)
> "frontend\.env.local" echo NEXT_PUBLIC_API_URL=http://localhost:!BACKEND_PORT!/api
echo [info] Backend port !BACKEND_PORT! - frontend port 3000
echo.

REM ------------------------------------------------------------------ launch
start "EDI backend  - close this window to stop"  cmd /k "cd /d "%~dp0" && set PYTHONIOENCODING=utf-8&& .venv\Scripts\python.exe -m uvicorn backend.main:app --host 0.0.0.0 --port !BACKEND_PORT!"
start "EDI frontend - close this window to stop"  cmd /k "cd /d "%~dp0frontend" && npm run dev"

echo [info] Waiting for the backend to become healthy ...
set /a TRIES=0
:waitloop
set /a TRIES+=1
curl -s -o nul http://localhost:!BACKEND_PORT!/api/health
if not errorlevel 1 goto ready
if !TRIES! GEQ 90 (
    echo [WARN] The backend did not answer within 90s - check the backend window for errors.
    goto open
)
ping -n 2 127.0.0.1 >nul
goto waitloop

:ready
echo [ok]   Backend is up.

:open
echo [info] Waiting for the frontend to compile ...
set /a TRIES=0
:waitfront
set /a TRIES+=1
curl -s -o nul http://localhost:3000/login
if not errorlevel 1 goto launched
if !TRIES! GEQ 120 (
    echo [WARN] The frontend did not answer within 120s - check the frontend window.
    goto launched
)
ping -n 2 127.0.0.1 >nul
goto waitfront

:launched
start "" http://localhost:3000
echo.
echo   ------------------------------------------------------------------
echo     App    http://localhost:3000     sign in: admin@example.com / admin123
echo     API    http://localhost:!BACKEND_PORT!/api/docs
echo     Docs   docs\how-it-works.html
echo   ------------------------------------------------------------------
echo.
echo   Close the two server windows to stop the application.
echo.
pause
endlocal
