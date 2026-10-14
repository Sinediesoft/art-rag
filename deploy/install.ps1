<#
一鍵安裝（Windows）：偵測這台電腦能跑到什麼程度，裝好套件、寫好 .env、下載模型、建索引，最後啟動並檢查。
重複執行沒關係：已經裝好的會略過，.env 裡你自己改過的設定不會被覆蓋。

  powershell -ExecutionPolicy Bypass -File deploy\install.ps1             # 自動判斷
  powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -DryRun     # 只列出會做什麼，不改任何東西
  powershell -ExecutionPolicy Bypass -File deploy\install.ps1 -Mode mock -NoStart

方案（-Mode auto 依顯卡記憶體選）：
  full  顯卡 12 GB 以上：Qwen3-VL 8B 主力、4B 備援
  light 顯卡 4～12 GB：4B（6 GB 以下回答不附圖）
  mock  沒有 NVIDIA 顯卡：不呼叫生成模型，只能看介面與流程
沒有 Docker 時資料庫改用檔案索引＋SQLite（功能相同）。3D 重建（Ortho2CAD）與排程（Java）是選裝，
沒有也能用，見 docs/5070ti-host.md 與 README。
缺少 uv、Node.js、Ollama、Docker 時會先列出來，問過才用 winget 安裝（-Yes 不問）。
#>
param(
    [ValidateSet('auto', 'full', 'light', 'mock')]
    [string]$Mode = 'auto',
    [switch]$Yes,
    [switch]$NoStart,
    [switch]$DryRun
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..')).Path
Set-Location $Repo

function Say([string]$text) { Write-Host "`n▶ $text" -ForegroundColor Cyan }
function Note([string]$text) { Write-Host "  $text" }
function Has([string]$cmd) { [bool](Get-Command $cmd -ErrorAction SilentlyContinue) }
# 原生指令失敗就停下來（PowerShell 5.1 不會因為結束碼不是 0 而停）
function Run([string]$exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$exe $($argList -join ' ') 失敗（結束碼 $LASTEXITCODE）" }
}
# winget 裝完後，這個視窗的 PATH 還是舊的：重新讀一次
function Refresh-Path {
    $env:Path = [Environment]::GetEnvironmentVariable('Path', 'Machine') + ';' + [Environment]::GetEnvironmentVariable('Path', 'User')
}
# 只看結束碼：PowerShell 5.1 在 Stop 模式下把導向的 stderr 當成錯誤中止（docker、nvidia-smi 會寫 stderr）
function Quiet([string]$exe, [string[]]$argList) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try { & $exe @argList *> $null; return ($LASTEXITCODE -eq 0) } catch { return $false } finally { $ErrorActionPreference = $prev }
}
function Ask([string]$question) {
    if ($Yes) { return $true }
    $answer = Read-Host "$question [y/N]"
    return $answer -match '^(y|yes)$'
}

# ---------------------------------------------------------------- 1. 偵測
Say '偵測這台電腦'
$vramMb = 0
$gpuName = $null
if (Has 'nvidia-smi') {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $line = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    $ErrorActionPreference = $prev
    if ($line) {
        $parts = $line -split ','
        $gpuName = $parts[0].Trim()
        $vramMb = [int]$parts[1].Trim()
    }
}
if ($gpuName) { Note "顯卡：$gpuName（$([math]::Round($vramMb / 1024, 1)) GB）" } else { Note '顯卡：沒有偵測到 NVIDIA 顯卡' }

if ($Mode -eq 'auto') {
    if ($vramMb -ge 12000) { $Mode = 'full' }
    elseif ($vramMb -ge 4000) { $Mode = 'light' }
    else { $Mode = 'mock' }
}
$smallGpu = ($Mode -eq 'light' -and $vramMb -lt 6000)
Note "方案：$Mode$(if ($smallGpu) { '（小顯卡，回答不附圖）' })"

# ---------------------------------------------------------------- 2. 系統軟體
# 名稱、指令、winget ID、是不是必要
$tools = @(
    @{ Name = 'uv（Python 套件管理）'; Cmd = 'uv'; Id = 'astral-sh.uv'; Required = $true },
    @{ Name = 'Node.js'; Cmd = 'node'; Id = 'OpenJS.NodeJS.LTS'; Required = $true },
    @{ Name = 'Ollama（本地模型）'; Cmd = 'ollama'; Id = 'Ollama.Ollama'; Required = ($Mode -ne 'mock') },
    @{ Name = 'Docker Desktop（資料庫，選裝；要管理員權限，裝完可能要重開機）'; Cmd = 'docker'; Id = 'Docker.DockerDesktop'; Required = $false }
)
$missing = @($tools | Where-Object { -not (Has $_.Cmd) -and ($_.Required -or $_.Cmd -eq 'docker') })
Say '檢查系統軟體'
foreach ($t in $tools) {
    $state = if (Has $t.Cmd) { '已安裝' } elseif ($t.Required) { '缺少（必要）' } else { '缺少（選裝）' }
    Note "$($t.Name)：$state"
}

if ($DryRun) {
    Say '只列出會做什麼（-DryRun）'
    if ($missing) { Note "會詢問是否用 winget 安裝：$(($missing | ForEach-Object { $_.Id }) -join '、')" }
    Note "寫 .env（方案 $Mode）："
    if (Has 'uv') {
        $dryArgs = @('run', '--no-project', 'python', 'deploy/configure_env.py', '--profile', $Mode, '--dry-run')
        if (-not (Has 'docker')) { $dryArgs += '--no-docker' }
        if ($smallGpu) { $dryArgs += '--small-gpu' }
        & uv @dryArgs
    }
    Note '接著：uv sync、npm ci、啟動資料庫、下載模型、建索引、建置前端、啟動並檢查'
    exit 0
}

foreach ($t in $missing) {
    if (-not (Has 'winget')) {
        Note "沒有 winget：請手動安裝 $($t.Name)（$($t.Id)）後再執行一次"
        if ($t.Required) { throw "缺少 $($t.Name)" }
        continue
    }
    if (Ask "要用 winget 安裝 $($t.Name) 嗎？") {
        Run 'winget' @('install', '--id', $t.Id, '-e', '--accept-package-agreements', '--accept-source-agreements')
        Refresh-Path
    } elseif ($t.Required) {
        throw "缺少 $($t.Name)，沒辦法繼續；裝好後再執行一次"
    }
}

# ---------------------------------------------------------------- 3. Docker（選裝）
$useDocker = $false
if (Has 'docker') {
    if (-not (Quiet 'docker' @('info'))) {
        $desktop = Join-Path $env:ProgramFiles 'Docker\Docker\Docker Desktop.exe'
        if (Test-Path $desktop) {
            Say '啟動 Docker Desktop（最多等 2 分鐘）'
            Start-Process $desktop
            for ($i = 0; $i -lt 40; $i++) {
                Start-Sleep 3
                if (Quiet 'docker' @('info')) { break }
            }
        }
    }
    $useDocker = Quiet 'docker' @('info')
}
if (-not $useDocker) { Note 'Docker 不能用：資料庫改用檔案索引＋SQLite（功能相同）' }

# ---------------------------------------------------------------- 4. .env
Say '寫 .env'
$cfgArgs = @('run', '--no-project', 'python', 'deploy/configure_env.py', '--profile', $Mode, '--set', 'HF_OFFLINE=false')
if (-not $useDocker) { $cfgArgs += '--no-docker' }
if ($smallGpu) { $cfgArgs += '--small-gpu' }
Run 'uv' $cfgArgs

# ---------------------------------------------------------------- 5. 套件
Say '安裝後端套件（uv sync）'
Push-Location backend
try { Run 'uv' @('sync', '--compile-bytecode') } finally { Pop-Location }
Say '安裝前端套件（npm ci）'
Push-Location frontend
try { Run 'npm' @('ci') } finally { Pop-Location }

# ---------------------------------------------------------------- 6. 資料庫
if ($useDocker) {
    Say '啟動資料庫（PostgreSQL 17＋pgvector）'
    if (-not (Quiet 'docker' @('compose', '-f', 'deploy/docker-compose.yml', '--env-file', '.env', 'up', '-d', '--wait', 'db'))) {
        throw '資料庫沒起來：確認 Docker Desktop 已開好，再執行一次'
    }
}

# ---------------------------------------------------------------- 7. 模型
if ($Mode -ne 'mock') {
    try { Invoke-RestMethod http://localhost:11434/api/version -TimeoutSec 3 | Out-Null }
    catch {
        $app = Join-Path $env:LOCALAPPDATA 'Programs\Ollama\ollama app.exe'
        if (Test-Path $app) { Start-Process $app } else { Start-Process ollama -ArgumentList 'serve' -WindowStyle Hidden }
        Start-Sleep 5
    }
    $models = if ($Mode -eq 'full') { @('qwen3-vl:8b-instruct', 'qwen3-vl:4b-instruct') } else { @('qwen3-vl:4b-instruct') }
    foreach ($m in $models) {
        Say "下載模型 $m（第一次要一段時間）"
        Run 'ollama' @('pull', $m)
    }
}

# ---------------------------------------------------------------- 8. 索引
Say '建索引（第一次會下載 bge-m3 約 2.2 GB、Chinese-CLIP 約 750 MB）'
Push-Location backend
try { Run 'uv' @('run', 'python', '..\pipelines\build_index.py') } finally { Pop-Location }
if ($Mode -ne 'mock') {
    # 模型都下載好了：之後只讀本機快取，斷網也能用
    Run 'uv' @('run', '--no-project', 'python', 'deploy/configure_env.py', '--profile', $Mode, '--set', 'HF_OFFLINE=true')
}

# ---------------------------------------------------------------- 9. 建置與啟動
Say '建置前端'
Push-Location frontend
try { Run 'npm' @('run', 'build') } finally { Pop-Location }

if ($NoStart) {
    Say '安裝完成（-NoStart）'
    Note '啟動：powershell -ExecutionPolicy Bypass -File deploy\windows\start-services.ps1 -SkipOrtho2cad -SkipScheduler'
    exit 0
}
Say '啟動並檢查'
if ($Mode -eq 'mock' -and -not (Has 'ollama')) {
    # start-services.ps1 一定要有 Ollama；mock 不需要，直接啟動後端（只綁本機）
    $logs = Join-Path $Repo 'data\logs'
    New-Item -ItemType Directory -Force $logs | Out-Null
    Start-Process -FilePath uv -ArgumentList 'run', '--project', 'backend', 'uvicorn', 'app.main:app', '--app-dir', 'backend', '--host', '127.0.0.1', '--port', '8000' `
        -WorkingDirectory $Repo -WindowStyle Hidden -RedirectStandardOutput (Join-Path $logs 'backend.out.log') -RedirectStandardError (Join-Path $logs 'backend.log')
    $ok = $false
    for ($i = 0; $i -lt 60; $i++) {
        Start-Sleep 3
        try { Invoke-RestMethod http://127.0.0.1:8000/api/v1/health -TimeoutSec 10 | Out-Null; $ok = $true; break } catch {}
    }
    if (-not $ok) { throw "後端沒起來，看 $logsackend.log" }
    Say "完成：打開 http://127.0.0.1:8000（方案 mock：只能看介面與流程）"
    exit 0
}
$startArgs = @('-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Repo 'deploy\windows\start-services.ps1'))
# 3D 重建、排程是選裝：沒有 llama.cpp 或 Java 就跳過，那兩頁會提示怎麼啟動
if (-not ($env:LLAMA_SERVER -or (Has 'llama-server'))) { $startArgs += '-SkipOrtho2cad' }
if (-not (Has 'java')) { $startArgs += '-SkipScheduler' }
Run 'powershell' $startArgs
Say "完成：打開 http://127.0.0.1:8000（方案 $Mode）"
