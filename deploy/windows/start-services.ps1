<#
直接在 Windows 跑整套展示（5070 Ti 主機等有 NVIDIA 顯示卡的電腦）：一個指令啟動所有服務，最後檢查健康狀態。
開機後執行一次即可；已經在跑的服務會略過。

  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1 -Stop      # 停掉這個腳本啟動的服務

依序：資料庫（.env 設了 DATABASE_URL 時，Docker）→ Ollama（:11434）→ Timefold 排程（:8082）→ Ortho2CAD llama-server（:8081，router 模式）→ 後端＋前端（:8000）。
- 都只綁 127.0.0.1；要給組員從 Tailscale 連，見 docs/5070ti-host.md
- Ortho2CAD 要 llama.cpp 的 Windows CUDA 版（winget 的是 Vulkan 版，比較慢）：-LlamaServer 指定，
  或設環境變數 LLAMA_SERVER，或放在 PATH，或解壓到 %USERPROFILE%\tools\llama.cpp-*\
- 紀錄寫在 data\logs\（不進 Git）
- 直接在 Windows 跑時，3D 重建產生的程式碼要在 WSL2 執行（.env 設 CAD_WSL_PYTHON，docs/adr/027）；
  這個腳本最後會檢查並預熱 WSL 裡的 CadQuery 環境，做法見 docs/5070ti-host.md
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

# 這台的 .env（資料庫、主力模型、keep_alive）
$envFile = Join-Path $Repo '.env'
$envVars = @{}
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding utf8 | Where-Object { $_ -match '^\s*([A-Z_]+)=(.*)$' } |
        ForEach-Object { $envVars[$Matches[1]] = $Matches[2].Trim() }
}

# 0. 資料庫（.env 設了 DATABASE_URL 才需要；docs/adr/009）：後端啟動時要連得到
if (-not $envVars['DATABASE_URL']) { '－ 沒設 DATABASE_URL：用檔案索引＋SQLite' }
elseif (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '設了 DATABASE_URL 但找不到 docker：先開 Docker Desktop' }
else {
    # docker 把進度訊息寫到 stderr；Windows PowerShell 5.1 在 Stop 模式下會把它當錯誤中止，這段改看結束碼
    $ErrorActionPreference = 'Continue'
    # Docker Desktop 剛開機時引擎還沒好：最多等 120 秒
    for ($i = 0; $i -lt 60; $i++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }
    docker compose -f deploy/docker-compose.yml --env-file .env up -d --wait db *> (Join-Path $Logs 'db.log')
    $dbOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if ($dbOk) { '✓ 資料庫（PostgreSQL＋pgvector）' } else { throw "資料庫沒起來，看 $Logs\db.log（Docker Desktop 有開嗎？）" }
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
    $backendArgs = @('run', '--project', 'backend', 'uvicorn', 'app.main:app', '--app-dir', 'backend',
        '--host', '127.0.0.1', '--port', '8000')
    Start-Hidden 'backend' 'uv' $backendArgs
    $code = Wait-Http 'http://127.0.0.1:8000/api/v1/health' 120
    if ($code -eq '000' -and -not (Test-Port 8000)) {
        # 系統記憶體極度吃緊（例如同時開遊戲）時，載入模型偶爾會當掉（0xc0000005）：重試一次
        '… 後端沒起來，重試一次（系統記憶體可能不夠，關掉其他大型程式會比較穩）'
        Start-Hidden 'backend' 'uv' $backendArgs
        $code = Wait-Http 'http://127.0.0.1:8000/api/v1/health' 120
    }
    "… 啟動後端：$code"
}

# 5. 預熱主力模型（docs/adr/025）：開機後第一題不用等載入（冷啟動首字 6–16 秒）
$model = if ($envVars['HYBRID_MODEL']) { $envVars['HYBRID_MODEL'] } else { 'qwen3-vl:4b-instruct' }
$keep = if ($envVars['MODEL_KEEP_ALIVE']) { $envVars['MODEL_KEEP_ALIVE'] } else { '5m' }
# 用 Invoke-RestMethod：Windows PowerShell 5.1 把 JSON 傳給 curl.exe 時會吃掉雙引號（HTTP 400）
$warm = @{ model = $model; keep_alive = $keep } | ConvertTo-Json -Compress
try {
    Invoke-RestMethod -Method Post -Uri http://127.0.0.1:11434/api/generate -ContentType 'application/json' `
        -Body ([Text.Encoding]::UTF8.GetBytes($warm)) -TimeoutSec 120 | Out-Null
    "… 預熱 $model（keep_alive $keep）：✓"
} catch {
    "－ 預熱 $model 失敗（$($_.Exception.Message)），第一題會比較慢"
}

# 5b. 3D 重建的執行環境（docs/adr/027）：產生的程式碼在 WSL2 執行；順便讓 WSL 開機，第一次 3D 重建不用多等
if (-not $envVars['CAD_WSL_PYTHON']) { '－ 沒設 CAD_WSL_PYTHON：3D 重建不執行模型產生的程式碼' }
else {
    $wslArgs = @()
    if ($envVars['CAD_WSL_DISTRO']) { $wslArgs += @('-d', $envVars['CAD_WSL_DISTRO']) }
    # 和沙箱一樣經過 unshare -rn：WSL 環境不允許非特權使用者命名空間時，這裡就會發現
    $wslArgs += @('--exec', '/usr/bin/unshare', '-rn', $envVars['CAD_WSL_PYTHON'], '-c', 'import cadquery')
    $ErrorActionPreference = 'Continue'
    wsl.exe @wslArgs *> (Join-Path $Logs 'cad-wsl.log')
    $cadOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if ($cadOk) { '✓ 3D 重建執行環境（WSL2 的 CadQuery）' }
    else { "✗ WSL 裡的 CadQuery 不能用，3D 重建會失敗：看 $Logs\cad-wsl.log（環境做法見 docs/5070ti-host.md）" }
}

# 6. 健康檢查
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
