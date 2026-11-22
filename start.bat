@echo off
REM Start JARVIS. Pass --build to serve the built interface from one port.
cd /d "%~dp0"
python start.py %*
if errorlevel 1 pause
