@echo off
setlocal

if not exist "%~dp0.venv\Scripts\python.exe" (
  echo VNC's local Python environment is missing. Run Setup VNC.bat first.
  pause
  exit /b 1
)

set "SMARTDATA_DB=%~dp0backend\smart_data.db"
set "SMARTDATA_ALLOW_DEMO_CREDIT=1"
set "SMARTDATA_ALLOW_DEMO_USAGE=1"
set "SMARTDATA_ALLOW_DEMO_WALLET=1"

"%~dp0.venv\Scripts\python.exe" "%~dp0backend\app.py"
if errorlevel 1 pause
