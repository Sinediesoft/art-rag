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
title Claude 設定檔診斷
if not exist "logs" mkdir "logs"
set "OUT=logs\claude_config_check.txt"
echo 診斷中，大約 10～30 秒，請稍等 ...
rem 只讀不改：檢查 Claude 的設定檔在哪、格式對不對，結果存到 logs\claude_config_check.txt 給教練讀
".venv\Scripts\python.exe" check_claude_config.py > "%OUT%" 2>&1
echo.>> "%OUT%"
echo ■ 正在執行的 Claude 程式位置>> "%OUT%"
powershell -NoProfile -Command "Get-Process -Name claude -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Path -Unique" >> "%OUT%" 2>&1
echo.>> "%OUT%"
echo ■ 電腦上安裝的 Claude（Store 版資訊）>> "%OUT%"
powershell -NoProfile -Command "Get-AppxPackage -Name *Claude* | Format-List Name,Version,InstallLocation" >> "%OUT%" 2>&1
type "%OUT%"
echo.
echo 結果也存在 logs\claude_config_check.txt，跟教練說「跑完了」就好
pause
