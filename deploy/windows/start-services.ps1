<#
直接在 Windows 跑整套展示（5070 Ti 主機等有 NVIDIA 顯示卡的電腦）：一個指令啟動所有服務，最後檢查健康狀態。
開機後執行一次即可；已經在跑的服務會略過。

  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1 -Stop      # 停掉這個腳本啟動的服務

依序：Ollama（:11434）→ Timefold 排程（:8082）→ Ortho2CAD llama-server（:8081，router 模式）→ 後端＋前端（:8000）。
- 都只綁 127.0.0.1；要給組員從 Tailscale 連，見 docs/5070ti-host.md
- Ortho2CAD 要 llama.cpp 的 Windows CUDA 版（winget 的是 Vulkan 版，比較慢）：-LlamaServer 指定，
  或設環境變數 LLAMA_SERVER，或放在 PATH，或解壓到 %USERPROFILE%\tools\llama.cpp-*\
- 紀錄寫在 data\logs\（不進 Git）
- 直接在 Windows 跑時 3D 重建不能執行產生的程式碼（沒有沙箱），要 WSL2，見 docs/5070ti-host.md
#>
param(
    [string]$LlamaServer = $env:LLAMA_SERVER,
    [switch]$SkipOrtho2cad,
    [switch]$SkipScheduler,
    [switch]$Stop
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Logs = Join-Path $Repo 'data\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
Set-Location $Repo

function Test-Port([int]$Port) {
    [bool](Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue)
}

function Wait-Http([string]$Url, [int]$Seconds = 90) {
    for ($i = 0; $i -lt $Seconds; $i++) {
        $code = curl.exe -s -o NUL -w '%{http_code}' $Url
        if ($code -ne '000') { return $code }
        Start-Sleep 1
    }
    return '000'
}

function Start-Hidden([string]$Name, [string]$Exe, [string[]]$Arguments) {
    Start-Process -FilePath $Exe -ArgumentList $Arguments -WorkingDirectory $Repo -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs "$Name.out.log") -RedirectStandardError (Join-Path $Logs "$Name.log") | Out-Null
}

if ($Stop) {
    foreach ($p in 8000, 8081, 8082) {
        Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -Confirm:$false; "已停止 :$p（pid $($_.OwningProcess)）" }
    }
    '（Ollama 不停：它是常駐服務，由 Ollama 自己管理）'
    return
}

# 1. Ollama
if (Test-Port 11434) { '✓ Ollama 已在執行' }
else {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { throw '找不到 ollama，請先安裝 Ollama' }
    Start-Hidden 'ollama' $ollama @('serve')
    "… 啟動 Ollama：$(Wait-Http 'http://127.0.0.1:11434/api/tags' 30)"
}

# 2. Timefold 排程服務（沒有也能跑：排程頁改用簡易排程）
if ($SkipScheduler) { '－ 略過排程服務' }
elseif (Test-Port 8082) { '✓ 排程服務已在執行' }
elseif (-not (Test-Path 'scheduler\target\scheduler.jar')) { '－ 沒有 scheduler\target\scheduler.jar（先跑 pipelines\setup_scheduler.py），排程頁改用簡易排程' }
else {
    $javaHome = (Get-Content 'scheduler\.java-home' -Raw).Trim()
    Start-Hidden 'scheduler' (Join-Path $javaHome 'bin\java.exe') @(
        '-XX:+UseSerialGC', '-Xms32m', '-Xmx768m', '-XX:MinHeapFreeRatio=10', '-XX:MaxHeapFreeRatio=30',
        '-jar', 'scheduler/target/scheduler.jar', '8082')
    "… 啟動排程服務：$(Wait-Http 'http://127.0.0.1:8082/health' 30)"
}

# 3. Ortho2CAD（llama-server router 模式，模型第一次 3D 重建時才載入）
if ($SkipOrtho2cad) { '－ 略過 Ortho2CAD' }
elseif (Test-Port 8081) { '✓ Ortho2CAD 已在執行' }
elseif (-not (Test-Path 'models\ortho2cad\mmproj-ortho2cad-f16.gguf')) { '－ 沒有 models\ortho2cad（先跑 pipelines\setup_ortho2cad.py），3D 重建頁會提示啟動' }
else {
    if (-not $LlamaServer) { $LlamaServer = (Get-Command llama-server -ErrorAction SilentlyContinue).Source }
    if (-not $LlamaServer) {
        $LlamaServer = Get-ChildItem (Join-Path $env:USERPROFILE 'tools\llama.cpp-*\llama-server.exe') -ErrorAction SilentlyContinue |
            Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
    }
    if (-not $LlamaServer) { '－ 找不到 llama-server（用 -LlamaServer 指定），略過 Ortho2CAD' }
    else {
        Start-Hidden 'ortho2cad' $LlamaServer @('--models-preset', 'deploy/llama-router.ini', '--models-max', '1',
            '--host', '127.0.0.1', '--port', '8081', '--no-webui')
        "… 啟動 Ortho2CAD（$LlamaServer）：$(Wait-Http 'http://127.0.0.1:8081/health' 30)"
    }
}

# 4. 後端＋前端（前端建置檔不在就先建置）
if (Test-Port 8000) { '✓ 後端已在執行' }
else {
    if (-not (Test-Path 'frontend\dist\index.html')) {
        '… 建置前端'
        Push-Location frontend; npm run build | Out-Null; Pop-Location
    }
    Start-Hidden 'backend' 'uv' @('run', '--project', 'backend', 'uvicorn', 'app.main:app', '--app-dir', 'backend',
        '--host', '127.0.0.1', '--port', '8000')
    "… 啟動後端：$(Wait-Http 'http://127.0.0.1:8000/api/v1/health' 120)"
}

# 5. 健康檢查
''
'健康檢查（http://127.0.0.1:8000/api/v1/health）'
try {
    $h = curl.exe -s http://127.0.0.1:8000/api/v1/health | ConvertFrom-Json
    "  狀態 $($h.status) · 索引一致 $($h.index_consistent) · 知識庫 $($h.manifest.kb_version)"
    foreach ($k in 'hybrid', 'hybrid_fallback', 'ortho2cad') {
        $s = $h.strategies.$k
        if ($s) { "  {0,-16} {1} {2}" -f $k, $(if ($s.available) { '✓' } else { '✗' }), "$($s.model)（$($s.detail)）" }
    }
    "  排程服務         $(if ($h.scheduler.available) { '✓ ' + $h.scheduler.engine } else { '✗ 改用簡易排程' })"
    if ($h.memory.gpu) { "  顯示記憶體       $($h.memory.gpu.percent)%（$($h.memory.gpu.name)）" }
    ''
    '打開 http://localhost:8000'
} catch {
    "✗ 後端沒有回應，看 $Logs\backend.log"
}
