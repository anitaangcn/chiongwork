@echo off
rem Double-click to open ChiongWork in your browser. Close this window to stop it.
title ChiongWork
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" call "%~dp0Setup.cmd" || exit /b 1
".venv\Scripts\python.exe" -m sgjobs web
