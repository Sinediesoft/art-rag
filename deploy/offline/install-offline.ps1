<#
離線安裝（docs/adr/046）：離線包根目錄的「安裝.cmd」呼叫，安裝過程不連網。
偵測顯卡選方案 → 寫 .env → 用離線包的 Python 與套件建環境 → 建索引 → 桌面捷徑 → 啟動並打開瀏覽器。
重複執行沒關係：已經做好的會略過，.env 裡你自己改過的設定不會被覆蓋（Ollama 位址與離線相關的設定除外）。

  安裝.cmd                  # 自動判斷
  安裝.cmd -Mode mock       # 指定方案：full、light、mock
  安裝.cmd -NoStart         # 裝完不啟動
  安裝.cmd -NoShortcut      # 不在桌面建捷徑

方案（-Mode auto 依顯卡記憶體選）：
  full  顯卡 12 GB 以上：Qwen3-VL 8B 主力、4B 備援
  light 顯卡 4～12 GB：4B（6 GB 以下回答不附圖）
  mock  沒有 NVIDIA 顯卡：不呼叫生成模型，只能看介面與流程
#>
param(
    [ValidateSet('auto', 'full', 'light', 'mock')]
    [string]$Mode = 'auto',
    [switch]$NoStart,
    [switch]$NoShortcut
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Repo
. (Join-Path $Repo 'deploy\offline\offline-env.ps1')
if (-not $IsOfflineBundle) {
    throw "找不到 $OfflineDir\offline.json：這支只給離線包用；從原始碼安裝請用 deploy\install.ps1"
}

# 失敗時交給 deploy/diagnose.py（docs/adr/045）；uv 與 Python 都在離線包裡，一開始就能用
$RunStart = [DateTimeOffset]::Now.ToUnixTimeSeconds()
$servicesStarted = $false
trap {
    # 啟動那一步失敗時 start-services.ps1 自己已經診斷過，不重複
    if (-not $servicesStarted) {
        $ErrorActionPreference = 'Continue'
        & uv run --no-project python (Join-Path $Repo 'deploy\diagnose.py') --since $RunStart `
            --message $_.Exception.Message
    }
    break
}

function Say([string]$text) { Write-Host "`n▶ $text" -ForegroundColor Cyan }
function Note([string]$text) { Write-Host "  $text" }
# 原生指令失敗就停下來（PowerShell 5.1 不會因為結束碼不是 0 而停）
function Run([string]$exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$exe $($argList -join ' ') 失敗（結束碼 $LASTEXITCODE）" }
}

# ---------------------------------------------------------------- 1. 偵測
Say '偵測這台電腦'
$info = Get-Content (Join-Path $OfflineDir 'offline.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Note "離線包版本：$($info.commit)（$($info.created)）"
$drive = (Split-Path -Qualifier $Repo).TrimEnd(':')
$freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
# Python 環境與索引約 5 GB（套件從快取用硬連結，同一顆磁碟不會再佔一份）
if ($freeGb -lt 6) { throw "$($drive): 只剩 $freeGb GB，安裝還需要約 6 GB" }

$vramMb = 0
$gpuName = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
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

# ---------------------------------------------------------------- 2. .env
# 沒有 Docker：資料庫用 SQLite、背景工作不啟用；模型走離線包自己的 Ollama（另一個連接埠）
Say '寫 .env'
$ollamaUrl = "http://127.0.0.1:$OfflineOllamaPort/v1"
$cfgArgs = @('run', '--no-project', 'python', 'deploy/configure_env.py', '--profile', $Mode, '--no-docker',
    '--set', 'HF_OFFLINE=true', '--set', 'REDIS_URL=',
    '--set', "HYBRID_BASE_URL=$ollamaUrl", '--set', "HYBRID_FALLBACK_BASE_URL=$ollamaUrl",
    '--set', "LORA_BASE_URL=$ollamaUrl")
if ($smallGpu) { $cfgArgs += '--small-gpu' }
Run 'uv' $cfgArgs

# ---------------------------------------------------------------- 3. Python 環境
Say '建立 Python 環境（從離線包的套件快取）'
Push-Location backend
try { Run 'uv' @('sync') } finally { Pop-Location }

# ---------------------------------------------------------------- 4. 索引
Say '建索引（用離線包裡的 bge-m3、Chinese-CLIP）'
Push-Location backend
try { Run 'uv' @('run', 'python', '..\pipelines\build_index.py') } finally { Pop-Location }

# ---------------------------------------------------------------- 5. 桌面捷徑
$bundleRoot = Split-Path $Repo -Parent
if ($NoShortcut) { Note '－ 不建桌面捷徑' }
else {
    Say '桌面捷徑'
    $desktop = [Environment]::GetFolderPath('Desktop')
    $shell = New-Object -ComObject WScript.Shell
    foreach ($pair in @(@('啟動 art-rag', '啟動.cmd'), @('停止 art-rag', '停止.cmd'))) {
        $link = $shell.CreateShortcut((Join-Path $desktop "$($pair[0]).lnk"))
        $link.TargetPath = Join-Path $bundleRoot $pair[1]
        $link.WorkingDirectory = $bundleRoot
        $link.Save()
        Note "$($pair[0]) → $($link.TargetPath)"
    }
}

# ---------------------------------------------------------------- 6. 啟動
if ($NoStart) {
    Say '安裝完成（-NoStart）'
    Note '啟動：點兩下桌面的「啟動 art-rag」，或離線包裡的「啟動.cmd」'
    exit 0
}
Say '啟動並檢查'
$servicesStarted = $true
Run 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File',
    (Join-Path $Repo 'deploy\windows\start-services.ps1'), '-SkipOrtho2cad', '-SkipScheduler')
Start-Process 'http://127.0.0.1:8000'
Say "完成：已打開 http://127.0.0.1:8000（方案 $Mode）"
Note '之後用桌面的「啟動 art-rag」「停止 art-rag」'
