@echo off
setlocal
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Run run_windows.bat first to prepare the Python environment.
  pause
  exit /b 1
)
if not defined PLAYWRIGHT_BROWSERS_PATH (
  if defined LOCALAPPDATA (
    set "PLAYWRIGHT_BROWSERS_PATH=%LOCALAPPDATA%\SEGIOArchive\Browsers"
  )
)
echo This download is optional. Installed Microsoft Edge or Chrome can be used instead.
echo Close any other browser installer windows before continuing.
".venv\Scripts\python.exe" -m playwright install chromium
if errorlevel 1 (
  echo.
  echo Browser download failed. Existing Edge or Chrome can still be used by run_windows.bat.
  echo If the message mentions _dir_lock, do not start another installer at the same time.
  pause
  exit /b 1
)
echo Browser installation completed. Run run_windows.bat to open the program.
pause
exit /b 0
