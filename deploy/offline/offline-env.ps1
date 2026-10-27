<#
離線包（docs/adr/046）的執行環境。start-services.ps1、install-offline.ps1 設好 $Repo 之後 dot-source：
  . (Join-Path $Repo 'deploy\offline\offline-env.ps1')
不是離線包（$Repo 旁邊沒有 offline\offline.json）時什麼都不做。
環境變數只設在這個 PowerShell 行程和它啟動的子行程，不改系統或使用者的設定。
#>

$OfflineDir = Join-Path (Split-Path $Repo -Parent) 'offline'
$IsOfflineBundle = Test-Path (Join-Path $OfflineDir 'offline.json')
# 離線包的 Ollama 用另一個連接埠：這台電腦原本就裝了 Ollama（佔著 11434）也不會打架
$OfflineOllamaPort = 11435

if ($IsOfflineBundle) {
    $env:Path = (Join-Path $OfflineDir 'uv') + ';' + (Join-Path $OfflineDir 'ollama') + ';' + $env:Path
    # Python 與套件都從離線包拿；UV_FROZEN：照 uv.lock 裝，不重新解析版本（離線也無從解析）
    $env:UV_CACHE_DIR = Join-Path $OfflineDir 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $OfflineDir 'python'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    # 這台另外裝的 Python 3.12 不用：套件快取是照離線包那份 Python 準備的
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_OFFLINE = '1'
    $env:UV_FROZEN = '1'
    $env:HF_HOME = Join-Path $OfflineDir 'hf'
    $env:HF_HUB_OFFLINE = '1'
    $env:OLLAMA_MODELS = Join-Path $OfflineDir 'ollama-models'
    $env:OLLAMA_HOST = "127.0.0.1:$OfflineOllamaPort"
}
