<#
離線包（docs/adr/046）的執行環境。start-services.ps1、install-offline.ps1 設好 $Repo 之後 dot-source：
  . (Join-Path $Repo 'deploy\offline\offline-env.ps1')
不是離線包（$Repo 旁邊沒有 offline\offline.json）時什麼都不做。
環境變數只設在這個 PowerShell 行程和它啟動的子行程，不改系統或使用者的設定。
#>

$OfflineDir = Join-Path (Split-Path $Repo -Parent) 'offline'
$IsOfflineBundle = Test-Path (Join-Path $OfflineDir 'offline.json')

# Ortho2CAD 用哪一份 llama.cpp：CUDA 13 版只支援 RTX 20 系列以後（compute capability 7.5 以上），
# GTX 10 系列等舊卡一載入模型就閃退（2026-10-30 學校電腦 GTX 1050 Ti）；也要 580 版以後的驅動程式。
# 舊卡、舊驅動、沒有 NVIDIA 的電腦用 Vulkan 版
function Select-OfflineLlama {
    if (-not (Test-Path (Join-Path $OfflineDir 'llama.cpp-vulkan'))) { return 'llama.cpp' }
    $cap = 0.0
    $driver = 0
    if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
        $prev = $ErrorActionPreference
        $ErrorActionPreference = 'Continue'
        $line = & nvidia-smi --query-gpu=compute_cap,driver_version --format=csv,noheader 2>$null | Select-Object -First 1
        $ErrorActionPreference = $prev
        $parts = "$line" -split ','
        if ($parts.Count -ge 2) {
            [double]::TryParse($parts[0].Trim(), [Globalization.NumberStyles]::Float, [Globalization.CultureInfo]::InvariantCulture, [ref]$cap) | Out-Null
            [int]::TryParse(($parts[1].Trim() -split '\.')[0], [ref]$driver) | Out-Null
        }
    }
    if ($cap -ge 7.5 -and $driver -ge 580) { return 'llama.cpp' } else { return 'llama.cpp-vulkan' }
}
# 離線包的 Ollama 用另一個連接埠：這台電腦原本就裝了 Ollama（佔著 11434）也不會打架
$OfflineOllamaPort = 11435
# 取代 Docker 的 WSL 環境（PostgreSQL＋pgvector、Redis、3D 重建的沙箱），裡面的設定見 wsl-setup.sh
$OfflineWslDistro = 'artrag'
# 不用預設的 5432／6379：電腦上另外裝的 PostgreSQL、Redis 會先佔走，連到別人的資料庫（2026-10-30 學校電腦）。
# 也不用 49152 以上：Hyper-V／WSL 常把那一段保留起來
$OfflineDbPort = 15432
$OfflineRedisPort = 16379
$OfflineWslPorts = @($OfflineDbPort, $OfflineRedisPort)

if ($IsOfflineBundle) {
    $env:Path = (Join-Path $OfflineDir 'uv') + ';' + (Join-Path $OfflineDir 'ollama') + ';' + $env:Path
    # Python 與套件都從離線包拿。不設 UV_FROZEN：uv.lock 和 pyproject 一致時檢查不用連網，
    # 設了反而每次 uv run --no-project（診斷、寫 .env）都印一行「--frozen has no effect」的警告
    $env:UV_CACHE_DIR = Join-Path $OfflineDir 'uv-cache'
    $env:UV_PYTHON_INSTALL_DIR = Join-Path $OfflineDir 'python'
    $env:UV_PYTHON_DOWNLOADS = 'never'
    # 這台另外裝的 Python 3.12 不用：套件快取是照離線包那份 Python 準備的
    $env:UV_PYTHON_PREFERENCE = 'only-managed'
    $env:UV_OFFLINE = '1'
    $env:HF_HOME = Join-Path $OfflineDir 'hf'
    $env:HF_HUB_OFFLINE = '1'
    $env:OLLAMA_MODELS = Join-Path $OfflineDir 'ollama-models'
    $env:OLLAMA_HOST = "127.0.0.1:$OfflineOllamaPort"
    $env:LLAMA_SERVER = Join-Path $OfflineDir "$(Select-OfflineLlama)\llama-server.exe"
}

# 本機連接埠連得上就算起來了。WSL 的服務經 localhost 轉送，Windows 這邊不一定看得到 Listen 狀態，所以直接連連看
function Test-TcpPort([int]$Port) {
    $client = New-Object System.Net.Sockets.TcpClient
    try { return $client.ConnectAsync('127.0.0.1', $Port).Wait(500) -and $client.Connected }
    catch { return $false }
    finally { $client.Dispose() }
}

# 帶著 WSL 服務的 wsl.exe 行程（命令列有 artrag-services）
function Get-OfflineWslKeeper {
    Get-CimInstance Win32_Process -Filter "Name='wsl.exe'" | Where-Object { $_.CommandLine -like '*artrag-services*' }
}

# 誰在聽這個連接埠（程式名稱）；沒人聽回 $null
function Get-PortOwner([int]$Port) {
    $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction SilentlyContinue | Select-Object -First 1
    if (-not $conn) { return $null }
    $proc = Get-Process -Id $conn.OwningProcess -ErrorAction SilentlyContinue
    if ($proc) { return $proc.ProcessName } else { return "PID $($conn.OwningProcess)" }
}

# artrag.tar 打包時用 5432／6379：匯入後改成 $OfflineWslPorts。重複執行沒關係；改了就先停掉舊的服務
function Set-OfflineWslPorts {
    $check = "grep -q '^port = $OfflineDbPort' /etc/postgresql/*/main/postgresql.conf && grep -q '^port $OfflineRedisPort' /etc/redis/artrag.conf"
    & wsl.exe -d $OfflineWslDistro -u root --exec /bin/sh -c $check *> $null
    if ($LASTEXITCODE -eq 0) { return }
    Stop-OfflineWsl
    $set = "sed -i -E 's/^#?port = [0-9]+/port = $OfflineDbPort/' /etc/postgresql/*/main/postgresql.conf && sed -i -E 's/^port [0-9]+/port $OfflineRedisPort/' /etc/redis/artrag.conf"
    & wsl.exe -d $OfflineWslDistro -u root --exec /bin/sh -c $set *> $null
    if ($LASTEXITCODE -ne 0) { throw "WSL 環境 $OfflineWslDistro 的連接埠設定失敗（結束碼 $LASTEXITCODE）" }
}

# 起 WSL 環境裡的 PostgreSQL 與 Redis，等到兩個連接埠都連得上；回傳是否成功
function Start-OfflineWsl([string]$LogDir, [int]$Seconds = 60) {
    if (-not (Get-OfflineWslKeeper)) {
        # 還沒起就有人在聽：是電腦上別的程式，連過去會變成「密碼錯誤」，不如直接講清楚
        foreach ($port in $OfflineWslPorts) {
            $owner = Get-PortOwner $port
            if ($owner -and $owner -ne 'wslrelay') {
                throw "連接埠 $port 被這台電腦上的其他程式（$owner）佔用，萊問的資料庫沒辦法啟動。請關掉 $owner 再按「重試」"
            }
        }
        Start-Process -FilePath 'wsl.exe' -WindowStyle Hidden `
            -ArgumentList @('-d', $OfflineWslDistro, '-u', 'root', '--exec', '/usr/local/bin/artrag-services') `
            -RedirectStandardOutput (Join-Path $LogDir 'wsl-services.out.log') `
            -RedirectStandardError (Join-Path $LogDir 'wsl-services.log') | Out-Null
    }
    for ($i = 0; $i -lt $Seconds; $i++) {
        if (@($OfflineWslPorts | Where-Object { -not (Test-TcpPort $_) }).Count -eq 0) { return $true }
        Start-Sleep 1
    }
    return $false
}

function Stop-OfflineWsl {
    Get-OfflineWslKeeper | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    wsl.exe --terminate $OfflineWslDistro *> $null
    $ErrorActionPreference = $prev
}
