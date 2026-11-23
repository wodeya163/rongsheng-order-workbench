@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=D:\Program Files\Python313\python.exe"
if not exist "%PYEXE%" set "PYEXE=python"
"%PYEXE%" build_v2.py %*
echo.
pause
