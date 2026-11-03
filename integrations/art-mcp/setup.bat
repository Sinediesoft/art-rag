@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"
set "PYTHONUTF8=1"
set "HF_HUB_DISABLE_SYMLINKS_WARNING=1"
title art-mcp 環境安裝
echo ============================================
echo   art-mcp 環境安裝（第一次約 3～10 分鐘）
echo ============================================
echo.

rem 所有工具都裝在這個資料夾裡，避免使用者名稱有中文造成問題
set "TOOLS=%~dp0.tools"
set "UV_INSTALL_DIR=%TOOLS%\uv"
set "UV_NO_MODIFY_PATH=1"
set "UV_PYTHON_INSTALL_DIR=%TOOLS%\python"
set "UV_CACHE_DIR=%TOOLS%\cache"
set "UV_PYTHON_PREFERENCE=only-managed"
set "UV_LINK_MODE=copy"

rem ---- 步驟 1：準備 uv（安裝 Python 和套件的工具）----
set "UV="
if exist "%UV_INSTALL_DIR%\uv.exe" set "UV=%UV_INSTALL_DIR%\uv.exe"
if not defined UV if exist "%UV_INSTALL_DIR%\bin\uv.exe" set "UV=%UV_INSTALL_DIR%\bin\uv.exe"
if not defined UV (
  echo [1/4] 下載 uv ...
  powershell -NoProfile -ExecutionPolicy ByPass -Command "irm https://astral.sh/uv/install.ps1 | iex"
)
if not defined UV if exist "%UV_INSTALL_DIR%\uv.exe" set "UV=%UV_INSTALL_DIR%\uv.exe"
if not defined UV if exist "%UV_INSTALL_DIR%\bin\uv.exe" set "UV=%UV_INSTALL_DIR%\bin\uv.exe"
if not defined UV (
  echo [失敗] uv 沒有安裝成功。
  goto :fail
)
echo [1/4] uv 已就緒
"%UV%" --version

rem ---- 步驟 2：建立 Python 3.12 環境（放在 .venv 資料夾）----
echo.
echo [2/4] 建立 Python 3.12 環境 ...
if not exist ".venv\Scripts\python.exe" "%UV%" venv .venv --python 3.12
if not exist ".venv\Scripts\python.exe" goto :fail

rem ---- 步驟 3：安裝固定版本的套件 ----
echo.
echo [3/4] 安裝套件（mcp 2.3.0、fastembed 0.9.0 等，版本都已固定）...
"%UV%" pip install --python ".venv\Scripts\python.exe" -r requirements.lock.txt
if errorlevel 1 goto :fail

rem ---- 步驟 4：檢查並產生 Claude 設定 ----
echo.
echo [4/4] 檢查環境 ...
".venv\Scripts\python.exe" check_env.py
if errorlevel 1 goto :fail

echo.
echo ============================================
echo   安裝完成！接下來回到 Claude 對話，告訴老師「setup 成功」。
echo ============================================
pause
exit /b 0

:fail
echo.
echo ============================================
echo   安裝沒有完成。請把整個視窗截圖給 Claude。
echo ============================================
pause
exit /b 1
