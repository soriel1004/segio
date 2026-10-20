@echo off
setlocal
cd /d "%~dp0"
set "PLAYWRIGHT_BROWSERS_PATH=%CD%\.browser"
if not exist ".venv\Scripts\python.exe" (
  py -3.12 -m venv .venv
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m segio_archive
if errorlevel 1 goto failed
exit /b 0
:failed
echo.
echo Installation or startup failed. Install Python 3.12 x64 and check Internet access.
pause
exit /b 1
