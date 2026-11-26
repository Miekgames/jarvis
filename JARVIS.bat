@echo off
REM JARVIS: double-click to start.
REM
REM The first run sets up a private Python environment in .venv (a few minutes
REM and a few hundred MB of packages); after that it starts straight away.
REM The interface comes built in a release download, so Node.js isn't needed.
setlocal
title JARVIS
cd /d "%~dp0"

if exist ".venv\.ready" goto run

REM -- find Python 3.9 or newer ----------------------------------------------
set "PY="
py -3 -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=py -3"
if not defined PY python -c "import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul && set "PY=python"
if not defined PY goto nopython

echo.
echo   Setting up JARVIS for the first time.
echo   This installs its Python packages into .venv here and takes a few minutes.
echo.
if exist ".venv" rmdir /s /q ".venv"
%PY% -m venv .venv
if errorlevel 1 goto failed
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -q --upgrade pip
".venv\Scripts\python.exe" -m pip install --disable-pip-version-check -r requirements.txt
if errorlevel 1 goto failed
echo ready> ".venv\.ready"

:run
".venv\Scripts\python.exe" start.py --prebuilt %*
if errorlevel 1 pause
exit /b

:nopython
echo.
echo   JARVIS needs Python 3.9 or newer, and it isn't installed.
echo.
echo   Install it from https://www.python.org/downloads/
echo   (tick "Add python.exe to PATH" in the installer), or in a terminal run:
echo.
echo       winget install Python.Python.3.12
echo.
echo   Then double-click JARVIS.bat again.
echo.
pause
exit /b 1

:failed
echo.
echo   Setup didn't finish - see the messages above. Check your internet
echo   connection and double-click JARVIS.bat to try again.
echo.
pause
exit /b 1
