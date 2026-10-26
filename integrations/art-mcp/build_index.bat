@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
if not exist ".venv\Scripts\python.exe" (
  echo 還沒安裝環境：請先雙擊 setup.bat
  pause
  exit /b 1
)
title art-mcp 建立索引
".venv\Scripts\python.exe" build_index.py
echo.
pause
