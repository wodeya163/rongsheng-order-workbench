@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYEXE=D:\Program Files\Python313\python.exe"
if exist "%PYEXE%" (
    "%PYEXE%" -m v2.app_v2 %*
) else (
    python -m v2.app_v2 %*
)
if errorlevel 1 (
    echo.
    echo Startup failed. Press any key to exit.
    pause >nul
)
