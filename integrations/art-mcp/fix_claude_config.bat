@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
title 修正 Claude 設定檔
if not exist "logs" mkdir "logs"
echo 這個程式會自動做三件事：
echo   1. 關閉 Claude（在 Claude 裡跑的工作會中斷；雲端的對話不會不見）
echo   2. 備份 Claude 設定檔，再把 MCP 設定加進去（原本內容不動）
echo   3. 重新打開 Claude
echo.
pause
rem 先關 Claude：Claude 開著時可能用它記憶中的舊內容蓋掉設定檔
taskkill /F /IM claude.exe >nul 2>&1
timeout /t 3 /nobreak >nul
".venv\Scripts\python.exe" fix_claude_config.py > "logs\fix_claude_config.txt" 2>&1
set "RC=%errorlevel%"
type "logs\fix_claude_config.txt"
if not "%RC%"=="0" goto failed
powershell -NoProfile -Command "$a = Get-StartApps | Where-Object { $_.AppID -like 'Claude_*' } | Select-Object -First 1; if ($a) { Start-Process ('shell:AppsFolder\' + $a.AppID); exit 0 } else { exit 1 }"
if errorlevel 1 echo 沒辦法自動打開 Claude，請從開始選單自己打開。
echo.
echo 完成。Claude 打開後，回到教練的對話說「好了」。
pause
exit /b 0
:failed
echo.
echo 修正沒有完成，設定檔沒被改壞。Claude 先不重開，跟教練說一聲就好。
pause
exit /b 1
