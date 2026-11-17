@echo off
rem ChiongWork command line, e.g.  cw search "data analyst"
"%~dp0.venv\Scripts\python.exe" -m sgjobs %*
