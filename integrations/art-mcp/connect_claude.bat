@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
title 一鍵接上 Claude 桌面版（art-rag MCP）
if not exist "logs" mkdir "logs"
echo ============================================
echo   一鍵接上 Claude 桌面版（art-rag MCP）
echo   1 安裝環境  2 建立索引  3 寫入 Claude 設定並重開 Claude
echo   第一次約 5～10 分鐘；之後再跑只要 1 分鐘
echo ============================================
echo.

echo [1/3] 安裝環境 ...
call setup.bat <nul
if errorlevel 1 goto :fail
echo   ✔ 環境就緒

echo [2/3] 建立索引（第一次會下載約 90 MB 的中文向量模型）...
".venv\Scripts\python.exe" build_index.py
if errorlevel 1 goto :fail

echo [3/3] 寫入 Claude 設定（會先關掉 Claude，原本的設定會備份、不會被蓋掉）...
taskkill /F /IM claude.exe >nul 2>&1
timeout /t 3 /nobreak >nul
".venv\Scripts\python.exe" fix_claude_config.py
if errorlevel 1 goto :fail

rem 重開 Claude：Store 版用 AppID，官網版用安裝路徑
powershell -NoProfile -Command "$a = Get-StartApps | Where-Object { $_.AppID -like 'Claude_*' } | Select-Object -First 1; if ($a) { Start-Process ('shell:AppsFolder\' + $a.AppID); exit 0 }; $p = Join-Path $env:LOCALAPPDATA 'AnthropicClaude\claude.exe'; if (Test-Path $p) { Start-Process $p; exit 0 }; exit 1"
if errorlevel 1 echo   沒辦法自動打開 Claude，請從開始選單自己打開。

echo.
echo ============================================
echo   ✔ 完成！Claude 打開後，輸入框旁的「＋」選單裡
echo     看得到 art-rag 就接上了。試問：團鶴版印彩紙的圖案可以商用嗎？
echo ============================================
pause
exit /b 0

:fail
echo.
echo ============================================
echo   ✘ 沒有完成（Claude 原本的設定沒被改壞）。
echo     請把這個視窗截圖，紀錄在 logs 資料夾。
echo ============================================
pause
exit /b 1
