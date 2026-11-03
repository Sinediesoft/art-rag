@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" (
  echo 還沒安裝環境：請先雙擊 setup.bat
  pause
  exit /b 1
)
title 考試小幫手 條件 B
".venv\Scripts\python.exe" exam_helper.py B
echo.
pause
