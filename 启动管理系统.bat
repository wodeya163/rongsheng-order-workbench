@echo off
chcp 65001 >nul
cd /d "%~dp0"
start "" "D:\Program Files\Tencent\Marvis\MarvisAgent\1.0.1100.151\runtime\python311\python.exe" "%~dp0product_manager.py"
