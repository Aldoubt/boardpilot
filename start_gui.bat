@echo off
setlocal
cd /d "%~dp0"
python --version >nul 2>&1
if errorlevel 1 (
  echo Python 3.8+ is required. Install Python and enable Add to PATH.
  pause
  exit /b 1
)
if not exist ".venv\Scripts\python.exe" (
  python -m venv --system-site-packages .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -c "import tkinter, paramiko" >nul 2>&1
if errorlevel 1 (
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" deployer_gui.py
if errorlevel 1 goto failed
exit /b 0
:failed
echo Startup failed. Check Python, tkinter and network access for dependencies.
pause
exit /b 1
