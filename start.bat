@echo off
chcp 65001 >nul
cd /d "%~dp0"
title 元件库管家
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8

if not exist ".venv\Scripts\python.exe" (
  echo 首次运行：正在创建 Python 环境...
  py -3.12 -m venv .venv 2>nul
  if not exist ".venv\Scripts\python.exe" (
    py -3.11 -m venv .venv 2>nul
  )
  if not exist ".venv\Scripts\python.exe" (
    python -m venv .venv
  )
  if not exist ".venv\Scripts\python.exe" (
    echo.
    echo [错误] 没有找到 Python，请先安装 Python 3.11/3.12。
    pause
    exit /b 1
  )
)

".venv\Scripts\python.exe" -c "import fastapi, uvicorn" >nul 2>&1
if errorlevel 1 (
  echo 正在安装依赖，第一次需要几分钟...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
)

".venv\Scripts\python.exe" run.py
pause
