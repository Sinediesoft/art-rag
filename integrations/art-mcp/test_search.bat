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
title art-mcp 搜尋小測驗
set "HF_HUB_OFFLINE=1"
".venv\Scripts\python.exe" test_search.py
echo.
pause
