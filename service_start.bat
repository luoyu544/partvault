@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
if not exist "data" mkdir "data"
".venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8765 >> "data\service.log" 2>&1
