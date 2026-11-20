@echo off
setlocal

set "BASE_PYTHON=%LOCALAPPDATA%\Programs\Python\Python315\python.exe"
if not exist "%BASE_PYTHON%" (
  where python >nul 2>nul
  if errorlevel 1 (
    echo Python was not found. Install Python 3.15 or add python.exe to PATH, then retry.
    pause
    exit /b 1
  )
  set "BASE_PYTHON=python"
)

if not exist "%~dp0.venv\Scripts\python.exe" (
  "%BASE_PYTHON%" -m venv "%~dp0.venv"
  if errorlevel 1 (
    echo Could not create VNC's local Python environment.
    pause
    exit /b 1
  )
)

"%~dp0.venv\Scripts\python.exe" -m pip install -r "%~dp0backend\requirements.txt"
if errorlevel 1 (
  echo Could not install VNC's backend dependencies.
  pause
  exit /b 1
)

echo VNC setup complete. Start the app with Start VNC.bat.
pause
