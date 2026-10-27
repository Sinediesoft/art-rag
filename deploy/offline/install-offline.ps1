<#
離線安裝（docs/adr/046）：離線包根目錄的「安裝.cmd」呼叫，安裝過程不連網，裝完全部功能都能用。
偵測顯卡選方案 → 確認 WSL2 → 匯入 WSL 環境（PostgreSQL、Redis、3D 重建的沙箱）→ 寫 .env → 用離線包的 Python 與套件建環境
→ 建索引 → 桌面捷徑 → 啟動全部服務並打開瀏覽器。
重複執行沒關係：已經做好的會略過，.env 裡你自己改過的設定不會被覆蓋（資料庫、模型位址等離線包的設定除外）。

這台還沒有 WSL2 時：會請你允許一次系統管理員權限，用離線包裡的檔案啟用 WSL，重新開機後再點一次「安裝.cmd」。

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
$Logs = Join-Path $Repo 'data\logs'
New-Item -ItemType Directory -Force $Logs | Out-Null

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
# wsl.exe 的訊息是 UTF-16、會寫 stderr：只看結束碼
function Invoke-Wsl([string[]]$argList) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $output = & wsl.exe @argList 2>&1 | ForEach-Object { "$_" -replace "`0", '' }
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    return @{ Code = $code; Output = ($output -join "`n").Trim() }
}

# ---------------------------------------------------------------- 1. 偵測
Say '偵測這台電腦'
$info = Get-Content (Join-Path $OfflineDir 'offline.json') -Raw -Encoding UTF8 | ConvertFrom-Json
Note "離線包版本：$($info.commit)（$($info.created)）"
$drive = (Split-Path -Qualifier $Repo).TrimEnd(':')
$freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
# Python 環境、WSL 環境、索引約 8 GB（套件從快取用硬連結，同一顆磁碟不會再佔一份）
if ($freeGb -lt 10) { throw "$($drive): 只剩 $freeGb GB，安裝還需要約 10 GB" }

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
if ($gpuName) { Note "顯卡：$gpuName（$([math]::Round($vramMb / 1024, 1)) GB）" } else { Note '顯卡：沒有偵測到 NVIDIA 顯卡（3D 重建會用 CPU，很慢）' }
if ($Mode -eq 'auto') {
    if ($vramMb -ge 12000) { $Mode = 'full' }
    elseif ($vramMb -ge 4000) { $Mode = 'light' }
    else { $Mode = 'mock' }
}
$smallGpu = ($Mode -eq 'light' -and $vramMb -lt 6000)
Note "方案：$Mode$(if ($smallGpu) { '（小顯卡，回答不附圖）' })"

# ---------------------------------------------------------------- 2. WSL2
# 3D 重建產生的程式碼要在 Linux 沙箱執行（ADR 027），資料庫與 Redis 也跑在同一個 WSL 環境，所以一定要 WSL2
Say '確認 WSL2'
$cs = Get-CimInstance Win32_ComputerSystem
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
# Hyper-V 已經在跑時 VirtualizationFirmwareEnabled 會回 False，要一起看 HypervisorPresent
$virtOk = $cs.HypervisorPresent -or $cpu.VirtualizationFirmwareEnabled
if (-not $virtOk) {
    throw 'BIOS 的虛擬化功能沒有開（WSL2 需要）：重開機進 BIOS，開啟 Intel VT-x 或 AMD SVM，再點一次「安裝.cmd」'
}
$status = Invoke-Wsl @('--status')
if ($status.Code -ne 0) {
    $msi = Get-ChildItem (Join-Path $OfflineDir 'wsl') -Filter '*.msi' | Select-Object -First 1
    Note '這台還沒有 WSL2：用離線包裡的檔案啟用，需要系統管理員權限（會跳出確認視窗）'
    # 啟用 Windows 內建的兩個功能（不用網路）＋安裝 WSL 本體；完成後要重新開機
    $elevated = @"
`$ErrorActionPreference = 'Continue'
dism.exe /online /enable-feature /featurename:VirtualMachinePlatform /all /norestart
dism.exe /online /enable-feature /featurename:Microsoft-Windows-Subsystem-Linux /all /norestart
Start-Process msiexec.exe -ArgumentList '/i', '"$($msi.FullName)"', '/quiet', '/norestart' -Wait
"@
    $script = Join-Path $env:TEMP 'artrag-enable-wsl.ps1'
    [IO.File]::WriteAllText($script, $elevated, (New-Object Text.UTF8Encoding $true))
    $p = Start-Process powershell -Verb RunAs -Wait -PassThru `
        -ArgumentList @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', "`"$script`"")
    Remove-Item $script -ErrorAction SilentlyContinue
    Say 'WSL2 已啟用：請重新開機，開機後再點一次「安裝.cmd」繼續安裝'
    exit 0
}
Note '✓ WSL2'

$distros = (Invoke-Wsl @('--list', '--quiet')).Output -split "`n" | ForEach-Object { $_.Trim() }
if ($distros -contains $OfflineWslDistro) { Note "✓ WSL 環境 $OfflineWslDistro 已經匯入" }
else {
    Say "匯入 WSL 環境 $OfflineWslDistro（PostgreSQL、Redis、3D 重建的沙箱）"
    # 虛擬磁碟放在離線包資料夾裡：要移除時先 wsl --unregister artrag，再刪資料夾
    $disk = Join-Path (Split-Path $Repo -Parent) 'wsl-data'
    $r = Invoke-Wsl @('--import', $OfflineWslDistro, $disk, (Join-Path $OfflineDir 'wsl\artrag.tar'), '--version', '2')
    if ($r.Code -ne 0) { throw "匯入 WSL 環境失敗：$($r.Output)" }
    Note "✓ 已匯入（$disk）"
}

# ---------------------------------------------------------------- 3. .env
# 資料庫與 Redis 在 WSL 環境裡（和 Docker 版同一組帳密，只綁本機）；模型走離線包自己的 Ollama（另一個連接埠）
Say '寫 .env'
$ollamaUrl = "http://127.0.0.1:$OfflineOllamaPort/v1"
$cfgArgs = @('run', '--no-project', 'python', 'deploy/configure_env.py', '--profile', $Mode,
    '--set', 'HF_OFFLINE=true',
    '--set', 'DATABASE_URL=postgresql://artrag:artrag@127.0.0.1:5432/artrag',
    '--set', 'REDIS_URL=redis://127.0.0.1:6379/0',
    '--set', 'CAD_WSL_PYTHON=/opt/artrag-cad/bin/python', '--set', "CAD_WSL_DISTRO=$OfflineWslDistro",
    '--set', "HYBRID_BASE_URL=$ollamaUrl", '--set', "HYBRID_FALLBACK_BASE_URL=$ollamaUrl",
    '--set', "LORA_BASE_URL=$ollamaUrl")
if ($smallGpu) { $cfgArgs += '--small-gpu' }
Run 'uv' $cfgArgs
# 排程服務用離線包的 Java（start-services.ps1 讀這個檔）
[IO.File]::WriteAllText((Join-Path $Repo 'scheduler\.java-home'), (Join-Path $OfflineDir 'jdk'), (New-Object Text.UTF8Encoding $false))

# ---------------------------------------------------------------- 4. Python 環境
Say '建立 Python 環境（從離線包的套件快取）'
Push-Location backend
try { Run 'uv' @('sync') } finally { Pop-Location }

# ---------------------------------------------------------------- 5. 資料庫與索引
Say '啟動 WSL 環境裡的 PostgreSQL、Redis'
if (-not (Start-OfflineWsl $Logs)) { throw "WSL 環境的 PostgreSQL／Redis 沒起來，看 $Logs\wsl-services.log" }
Note '✓ PostgreSQL :5432、Redis :6379'
Say '建索引並寫進資料庫（用離線包裡的 bge-m3、Chinese-CLIP）'
Push-Location backend
try { Run 'uv' @('run', 'python', '..\pipelines\build_index.py') } finally { Pop-Location }

# ---------------------------------------------------------------- 6. 桌面捷徑
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

# ---------------------------------------------------------------- 7. 啟動
if ($NoStart) {
    Say '安裝完成（-NoStart）'
    Note '啟動：點兩下桌面的「啟動 art-rag」，或離線包裡的「啟動.cmd」'
    exit 0
}
Say '啟動並檢查'
$servicesStarted = $true
Run 'powershell' @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', (Join-Path $Repo 'deploy\windows\start-services.ps1'))
Start-Process 'http://127.0.0.1:8000'
Say "完成：已打開 http://127.0.0.1:8000（方案 $Mode）"
Note '之後用桌面的「啟動 art-rag」「停止 art-rag」'
