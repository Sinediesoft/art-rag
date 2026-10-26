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
title art-mcp 健康檢查
set "HF_HUB_OFFLINE=1"
if not exist "logs" mkdir "logs"
echo 健康檢查中，大約 10～60 秒，請稍等 ...
rem 結果同時存一份到 logs\self_check_latest.txt，Claude 可以直接讀這個檔來幫你判斷
".venv\Scripts\python.exe" self_check.py > "logs\self_check_latest.txt" 2>&1
type "logs\self_check_latest.txt"
echo.
echo 這次的結果也存在 logs\self_check_latest.txt
pause
