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
title art-mcp 評估計分
".venv\Scripts\python.exe" score.py
if errorlevel 1 (
  echo.
  echo 計分沒有完成，請把這個視窗截圖給 Claude。
  pause
  exit /b 1
)
echo.
set /p OPEN=要打開結果圖表嗎？直接按 Enter 打開，輸入 n 再按 Enter 不打開：
if /i not "%OPEN%"=="n" start "" "eval\results\report.html"
