@echo off
REM Trading Chambers Phase 0 — Windows fallback launcher.
REM Runs the engine + dashboard and restarts it if the process exits for any reason.
REM Run from the repo root (this file lives in deploy\), or set CHAMBERS_HOME below.

setlocal
if "%CHAMBERS_HOME%"=="" set CHAMBERS_HOME=%~dp0..
cd /d "%CHAMBERS_HOME%"

if not exist ".venv\Scripts\python.exe" (
  echo [chambers] .venv not found. Run:  py -3.12 -m venv .venv ^&^& .venv\Scripts\pip install -r requirements.txt
  pause
  exit /b 1
)
if not exist ".env" (
  echo [chambers] .env not found. Copy .env.example to .env and fill in the paper keys.
  pause
  exit /b 1
)

:loop
echo [chambers] %date% %time% starting engine
".venv\Scripts\python.exe" -m chambers.main
echo [chambers] %date% %time% engine exited with code %errorlevel% - restarting in 10s
timeout /t 10 /nobreak >nul
goto loop
