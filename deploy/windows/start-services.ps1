<#
直接在 Windows 跑整套展示（5070 Ti 主機等有 NVIDIA 顯示卡的電腦）：一個指令啟動所有服務，最後檢查健康狀態。
開機後執行一次即可；已經在跑的服務會略過。

  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1
  powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1 -Stop      # 停掉這個腳本啟動的服務

依序：資料庫（.env 設了 DATABASE_URL 時，Docker）＋Redis（.env 設了 REDIS_URL 時）→ Ollama（:11434）→ Timefold 排程（:8082）→ Ortho2CAD llama-server（:8081，router 模式）→ 後端＋前端（:8000）→ 背景工作 worker（gpu、schedule）。
- 前端的 3D 重建與排程一律走背景工作（docs/adr/041）：沒有 Redis 與 worker 時這兩頁回 503 JOBS_UNAVAILABLE
- 都只綁 127.0.0.1；要給組員從 Tailscale 連，見 docs/5070ti-host.md
- Ortho2CAD 要 llama.cpp 的 Windows CUDA 版（winget 的是 Vulkan 版，比較慢）：-LlamaServer 指定，
  或設環境變數 LLAMA_SERVER，或放在 PATH，或解壓到 %USERPROFILE%\tools\llama.cpp-*\
- 紀錄寫在 data\logs\（不進 Git）
- 直接在 Windows 跑時，3D 重建產生的程式碼要在 WSL2 執行（.env 設 CAD_WSL_PYTHON，docs/adr/027）；
  這個腳本最後會檢查並預熱 WSL 裡的 CadQuery 環境，做法見 docs/5070ti-host.md
- 啟動失敗或健康檢查有 ✗ 時，deploy/diagnose.py 會看這次的 log 說明原因與修法（docs/adr/045；-NoDiagnose 關掉）
- 在離線包裡執行時（docs/adr/046），uv、Python、Ollama、模型都用離線包裡的；Ollama 用 .env 的 HYBRID_BASE_URL
  那個連接埠（離線包是 :11435），-Stop 也會停掉它
#>
param(
    [string]$LlamaServer = $env:LLAMA_SERVER,
    [switch]$SkipOrtho2cad,
    [switch]$SkipScheduler,
    [switch]$Stop,
    [switch]$NoDiagnose
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Logs = Join-Path $Repo 'data\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null
Set-Location $Repo
. (Join-Path $Repo 'deploy\offline\offline-env.ps1')

# 診斷只看這次執行之後寫的 log：上次留下的舊錯誤不算
$RunStart = [DateTimeOffset]::Now.ToUnixTimeSeconds()
$problems = @()

function Invoke-Diagnose([string]$Message) {
    if ($NoDiagnose -or -not (Get-Command uv -ErrorAction SilentlyContinue)) { return }
    $diagArgs = @('run', '--no-project', 'python', (Join-Path $Repo 'deploy\diagnose.py'), '--since', $RunStart)
    # Windows PowerShell 5.1 會把空字串參數整個吃掉，--message 後面就沒有值了
    if ($Message) { $diagArgs += @('--message', $Message) }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & uv @diagArgs
    $ErrorActionPreference = $prev
}

# 中途 throw（資料庫、migration、找不到 Ollama）：先診斷，再照原本的錯誤結束
trap {
    Invoke-Diagnose $_.Exception.Message
    break
}

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

function Start-Hidden([string]$Name, [string]$Exe, [string[]]$Arguments, [string]$Dir = $Repo) {
    Start-Process -FilePath $Exe -ArgumentList $Arguments -WorkingDirectory $Dir -WindowStyle Hidden `
        -RedirectStandardOutput (Join-Path $Logs "$Name.out.log") -RedirectStandardError (Join-Path $Logs "$Name.log") | Out-Null
}

# worker 沒有監聽埠，只能從命令列認：uv、celery.exe 啟動器與真正的 python 行程都帶著這些參數
function Get-Worker([string]$Queue) {
    Get-CimInstance Win32_Process -Filter "CommandLine LIKE '%app.jobs.celery_app%'" |
        Where-Object { -not $Queue -or $_.CommandLine -match "-Q $Queue\b" }
}

if ($Stop) {
    $ports = @(8000, 8081, 8082)
    # 離線包的 Ollama 是這個腳本自己起的 ollama serve，不是常駐服務：一起停
    if ($IsOfflineBundle) { $ports += $OfflineOllamaPort }
    foreach ($p in $ports) {
        Get-NetTCPConnection -LocalPort $p -State Listen -ErrorAction SilentlyContinue |
            ForEach-Object { Stop-Process -Id $_.OwningProcess -Force -Confirm:$false; "已停止 :$p（pid $($_.OwningProcess)）" }
    }
    Get-Worker | ForEach-Object {
        Stop-Process -Id $_.ProcessId -Force -Confirm:$false -ErrorAction SilentlyContinue; "已停止 worker（pid $($_.ProcessId)）"
    }
    if (-not $IsOfflineBundle) {
        '（資料庫與 Redis 容器不停：docker compose -f deploy/docker-compose.yml stop）'
        '（Ollama 不停：它是常駐服務，由 Ollama 自己管理）'
    }
    return
}

# 這台的 .env（資料庫、主力模型、keep_alive）
$envFile = Join-Path $Repo '.env'
$envVars = @{}
if (Test-Path $envFile) {
    Get-Content $envFile -Encoding utf8 | Where-Object { $_ -match '^\s*([A-Z_]+)=(.*)$' } |
        ForEach-Object { $envVars[$Matches[1]] = $Matches[2].Trim() }
}

# 背景工作和後端用同一個判斷（app.core.config 的 jobs_enabled）：REDIS_URL 和工作資料庫都有值才啟用
$jobsOn = [bool]($envVars['REDIS_URL'] -and ($envVars['JOBS_DATABASE_URL'] -or $envVars['DATABASE_URL']))

# 0. 資料庫（.env 設了 DATABASE_URL 才需要；docs/adr/009）：後端啟動時要連得到；Redis 是背景工作的 broker
$services = @()
if ($envVars['DATABASE_URL']) { $services += 'db' }
if ($jobsOn) { $services += 'redis' }
if (-not $envVars['DATABASE_URL']) { '－ 沒設 DATABASE_URL：用檔案索引＋SQLite' }
if (-not $jobsOn) { '－ 沒設 REDIS_URL：背景工作沒啟用，3D 重建與排程頁會回 503' }
if ($services.Count -gt 0) {
    if (-not (Get-Command docker -ErrorAction SilentlyContinue)) { throw '設了 DATABASE_URL／REDIS_URL 但找不到 docker：先開 Docker Desktop' }
    # docker 把進度訊息寫到 stderr；Windows PowerShell 5.1 在 Stop 模式下會把它當錯誤中止，這段改看結束碼
    $ErrorActionPreference = 'Continue'
    # Docker Desktop 剛開機時引擎還沒好：最多等 120 秒
    for ($i = 0; $i -lt 60; $i++) { docker info *> $null; if ($LASTEXITCODE -eq 0) { break }; Start-Sleep 2 }
    docker compose -f deploy/docker-compose.yml --env-file .env up -d --wait @services *> (Join-Path $Logs 'db.log')
    $dbOk = $LASTEXITCODE -eq 0
    $ErrorActionPreference = 'Stop'
    if ($dbOk) { "✓ 容器：$($services -join '、')" } else { throw "容器沒起來，看 $Logs\db.log（Docker Desktop 有開嗎？）" }
}

# 0b. 背景工作的 migration（可重複執行，已套用的會略過）：worker 和後端都要這些表
if ($jobsOn) {
    $ErrorActionPreference = 'Continue'
    Push-Location backend
    uv run python -m app.jobs.admin migrate *> (Join-Path $Logs 'jobs-migrate.log')
    $migOk = $LASTEXITCODE -eq 0
    Pop-Location
    $ErrorActionPreference = 'Stop'
    if ($migOk) { '✓ 背景工作資料表' } else { throw "背景工作的 migration 失敗，看 $Logs\jobs-migrate.log" }
}

# 1. Ollama：連接埠照 .env 的 HYBRID_BASE_URL（離線包是 11435）；指到別台主機時照舊在本機起 :11434
$ollamaPort = 11434
if ($envVars['HYBRID_BASE_URL'] -match '^https?://(127\.0\.0\.1|localhost):(\d+)') { $ollamaPort = [int]$Matches[2] }
$ollamaRoot = "http://127.0.0.1:$ollamaPort"
if (Test-Port $ollamaPort) { "✓ Ollama 已在執行（:$ollamaPort）" }
else {
    $ollama = (Get-Command ollama -ErrorAction SilentlyContinue).Source
    if (-not $ollama) { throw '找不到 ollama，請先安裝 Ollama' }
    # 使用者自己設了 OLLAMA_HOST（例如開放給組員）就照他的
    if (-not $env:OLLAMA_HOST -and $ollamaPort -ne 11434) { $env:OLLAMA_HOST = "127.0.0.1:$ollamaPort" }
    Start-Hidden 'ollama' $ollama @('serve')
    $code = Wait-Http "$ollamaRoot/api/tags" 30
    "… 啟動 Ollama（:$ollamaPort）：$code"
    if ($code -eq '000') { $problems += '啟動 Ollama：000（30 秒內沒有回應）' }
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

# 4b. 背景工作 worker（docs/adr/041）：gpu 跑 3D 重建、schedule 跑排程求解；--pool=solo 一次一個工作，
#     也是 Celery 在 Windows 上唯一可用的執行方式。要在 backend\ 執行，-A app.jobs.celery_app 才找得到
if ($jobsOn) {
    foreach ($q in 'gpu', 'schedule') {
        if (Get-Worker $q) { "✓ worker（$q）已在執行"; continue }
        Start-Hidden "worker-$q" 'uv' @('run', 'celery', '-A', 'app.jobs.celery_app', 'worker', '-Q', $q,
            '--pool=solo', '-n', "$q@%h", '-l', 'INFO') (Join-Path $Repo 'backend')
        "… 啟動 worker（$q）"
    }
}

# 5. 預熱主力模型（docs/adr/025）：開機後第一題不用等載入（冷啟動首字 6–16 秒）
$model = if ($envVars['HYBRID_MODEL']) { $envVars['HYBRID_MODEL'] } else { 'qwen3-vl:4b-instruct' }
$keep = if ($envVars['MODEL_KEEP_ALIVE']) { $envVars['MODEL_KEEP_ALIVE'] } else { '5m' }
# 用 Invoke-RestMethod：Windows PowerShell 5.1 把 JSON 傳給 curl.exe 時會吃掉雙引號（HTTP 400）
$warm = @{ model = $model; keep_alive = $keep } | ConvertTo-Json -Compress
# mock 方案不呼叫生成模型：預熱只會白白佔記憶體（沒有顯卡時是系統記憶體）
if ($envVars['LLM_MODE'] -eq 'mock') { '－ mock 方案：不預熱模型' }
else {
    try {
        Invoke-RestMethod -Method Post -Uri "$ollamaRoot/api/generate" -ContentType 'application/json' `
            -Body ([Text.Encoding]::UTF8.GetBytes($warm)) -TimeoutSec 120 | Out-Null
        "… 預熱 $model（keep_alive $keep）：✓"
    } catch {
        "－ 預熱 $model 失敗（$($_.Exception.Message)），第一題會比較慢"
    }
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
    else {
        "✗ WSL 裡的 CadQuery 不能用，3D 重建會失敗：看 $Logs\cad-wsl.log（環境做法見 docs/5070ti-host.md）"
        $problems += 'WSL 裡的 CadQuery 不能用'
    }
}

# 6. 健康檢查
#    公開的 /health 只回存活與就緒（docs/adr/030）；模型、記憶體與背景工作要看 /status，
#    它要有效的 JWT：先打 /auth/accounts 拿一張預設帳號（權限最低）的 cookie
''
'健康檢查（http://127.0.0.1:8000）'
$mark = { param($ok) if ($ok) { '✓' } else { '✗' } }
try {
    $web = $null
    $h = Invoke-RestMethod -UseBasicParsing http://127.0.0.1:8000/api/v1/health -TimeoutSec 15
    "  狀態 $($h.status) · 資料庫 $(& $mark $h.db) · 索引一致 $(& $mark $h.index_consistent) · 知識庫 $($h.kb_version)"
    Invoke-RestMethod -UseBasicParsing http://127.0.0.1:8000/api/v1/auth/accounts -SessionVariable web -TimeoutSec 15 | Out-Null
    $st = Invoke-RestMethod -UseBasicParsing http://127.0.0.1:8000/api/v1/status -WebSession $web -TimeoutSec 30
    foreach ($k in 'hybrid', 'hybrid_fallback', 'ortho2cad') {
        $s = $st.strategies.$k
        if ($s) { "  {0,-16} {1} {2}" -f $k, (& $mark $s.available), $s.model }
        # Ortho2CAD 是選裝，略過時本來就不可用，不算問題
        if ($s -and -not $s.available -and $k -ne 'ortho2cad') { $problems += "$k 模型不可用：$($s.model)" }
    }
    if ($st.memory.gpu) { "  顯示記憶體       $($st.memory.gpu.percent)%（$($st.memory.gpu.name)）" }
} catch {
    "✗ 後端沒有回應，看 $Logs\backend.log"
    $problems += "後端沒有回應（$($_.Exception.Message)）"
}

# 後端沒有排程引擎的狀態，直接問排程服務；連不上時排程頁改用簡易排程
try {
    $sch = Invoke-RestMethod -UseBasicParsing http://127.0.0.1:8082/health -TimeoutSec 5
    "  排程服務         ✓ $($sch.solver) $($sch.version)"
} catch {
    '  排程服務         ✗ 改用簡易排程'
}

# 6b. 背景工作：worker 啟動後會登記到 Redis（載入索引要十幾秒），最多等 90 秒兩個佇列都有人接
if ($jobsOn -and $web) {
    $jobs = $null
    $queues = @()
    for ($i = 0; $i -lt 30; $i++) {
        try {
            $jobs = (Invoke-RestMethod -UseBasicParsing http://127.0.0.1:8000/api/v1/status -WebSession $web -TimeoutSec 30).jobs
            $queues = @($jobs.workers | ForEach-Object { $_.queues }) | Sort-Object -Unique
        } catch { }
        if (('gpu' -in $queues) -and ('schedule' -in $queues)) { break }
        Start-Sleep 3
    }
    "  背景工作         資料庫 $(& $mark $jobs.database_ok) · Redis $(& $mark $jobs.broker_ok) · " +
        "worker gpu $(& $mark ('gpu' -in $queues)) schedule $(& $mark ('schedule' -in $queues))"
    if (-not (('gpu' -in $queues) -and ('schedule' -in $queues))) {
        "  ✗ worker 沒有登記，3D 重建或排程會一直排隊：看 $Logs\worker-gpu.log、worker-schedule.log"
        $problems += 'worker 沒有登記'
    }
}
# 沒有中斷、但健康檢查有 ✗：一起交給診斷
if ($problems) { Invoke-Diagnose ($problems -join '；') }
''
'打開 http://localhost:8000'
