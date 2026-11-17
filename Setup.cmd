@echo off
rem One-time setup for ChiongWork on Windows: creates a private Python environment and installs what it needs.
setlocal
cd /d "%~dp0"
title ChiongWork setup

set "PY="
where py >nul 2>nul && py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY (
  where python >nul 2>nul && python -c "import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)" >nul 2>nul && set "PY=python"
)
if not defined PY (
  echo.
  echo ChiongWork needs Python 3.11 or newer, and it wasn't found.
  echo Install it from https://www.python.org/downloads/  ^(tick "Add python.exe to PATH"^), then run Setup.cmd again.
  echo.
  pause
  exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
  echo Creating a private Python environment in .venv ...
  %PY% -m venv .venv || goto :fail
)
echo Installing the two libraries ChiongWork uses ^(rich, pypdf^) ...
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q -r requirements.txt || goto :fail

echo.
echo Setup done. Double-click ChiongWork.cmd to open the app.
echo.
pause
exit /b 0

:fail
echo.
echo Setup didn't finish. Check your internet connection and run Setup.cmd again.
pause
exit /b 1
