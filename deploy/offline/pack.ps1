<#
做完整版離線安裝包（docs/adr/046、047）：在已經裝好、可以連網的電腦（5070 Ti 主機）跑一次，輸出一個資料夾：
  setup.exe（安裝精靈）、使用說明.txt、payload\（app、offline、desktop：程式、模型與執行環境、桌面程式 art-rag.exe）
把整個資料夾複製到別台 Windows 電腦後點兩下 setup.exe；裝完從開始功能表或桌面打開 art-rag，
第一次打開會在程式視窗裡完成安裝（不連網），之後直接用。

  powershell -ExecutionPolicy Bypass -File deploy\offline\pack.ps1                         # 輸出到 D:\art-rag-offline
  powershell -ExecutionPolicy Bypass -File deploy\offline\pack.ps1 -Out E:\art-rag-offline

- 打包時會連網：可攜版 Python 3.12、Python 套件（照 backend\uv.lock）、前端套件（npm ci）、Timefold 排程的套件（Maven）、
  Ubuntu 24.04 的 WSL 映像與裡面的 PostgreSQL／Redis／CadQuery、WSL 的離線安裝程式、Electron（desktop\package-lock.json）
- 打包這台要有 Inno Setup 6（winget install JRSoftware.InnoSetup；-Iscc 指定 ISCC.exe），裝的電腦不用
- 從這台複製：Ollama 與 qwen3-vl 8B／4B、Hugging Face 快取裡 shared\models.yaml 釘選的 bge-m3、Chinese-CLIP、
  Ortho2CAD 模型與 llama.cpp（CUDA 版）、Java 21
- 只打包已 commit 的內容（git archive HEAD）：.env、data\ 不會帶出去
- 輸出約 25 GB；複製到隨身碟要用 exFAT 或 NTFS（8B 模型單一個檔案超過 4 GB，FAT32 放不下）
#>
param(
    [string]$Out = 'D:\art-rag-offline',
    [string[]]$Models = @('qwen3-vl:8b-instruct', 'qwen3-vl:4b-instruct'),
    [string]$Iscc
)

$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1 的下載進度條會讓 Invoke-WebRequest 慢好幾倍
$ProgressPreference = 'SilentlyContinue'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Repo

# Ubuntu 官方的 WSL 映像；同一個資料夾有 SHA256SUMS 可以核對
$RootfsBase = 'https://cloud-images.ubuntu.com/wsl/releases/24.04/current'
$RootfsName = 'ubuntu-noble-wsl-amd64-wsl.rootfs.tar.gz'
$BuildDistro = 'artrag-build'

function Say([string]$text) { Write-Host "`n▶ $text" -ForegroundColor Cyan }
function Note([string]$text) { Write-Host "  $text" }
# 原生指令失敗就停下來（PowerShell 5.1 不會因為結束碼不是 0 而停）
function Run([string]$exe, [string[]]$argList) {
    & $exe @argList
    if ($LASTEXITCODE -ne 0) { throw "$exe $($argList -join ' ') 失敗（結束碼 $LASTEXITCODE）" }
}
# winget 裝的指令可能是連結：複製連結指到的真正檔案
function Resolve-Exe([string]$name) {
    $cmd = Get-Command $name -ErrorAction SilentlyContinue
    if (-not $cmd) { return $null }
    $item = Get-Item $cmd.Source
    if ($item.LinkType -and $item.Target) { return @($item.Target)[0] }
    return $item.FullName
}
# C:\Users\me → /mnt/c/Users/me（WSL 預設掛載點）
function ConvertTo-WslPath([string]$path) {
    $full = [IO.Path]::GetFullPath($path)
    return '/mnt/' + $full.Substring(0, 1).ToLower() + ($full.Substring(2) -replace '\\', '/')
}
# wsl.exe 自己的訊息是 UTF-16：這裡只看結束碼
function Wsl([string[]]$argList) {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    & wsl.exe @argList
    $code = $LASTEXITCODE
    $ErrorActionPreference = $prev
    if ($code -ne 0) { throw "wsl.exe $($argList -join ' ') 失敗（結束碼 $code）" }
}
function Get-WslDistros {
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    $names = @(& wsl.exe --list --quiet 2>$null | ForEach-Object { ($_ -replace "`0", '').Trim() } | Where-Object { $_ })
    $ErrorActionPreference = $prev
    return $names
}

# ---------------------------------------------------------------- 0. 檢查
Say '檢查這台電腦'
foreach ($cmd in 'git', 'uv', 'npm', 'ollama', 'wsl') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "找不到 $cmd：打包要在已經裝好整套系統的電腦上跑" }
}
if ((Test-Path $Out) -and (Get-ChildItem $Out -Force | Select-Object -First 1)) {
    throw "$Out 已經有東西：換一個 -Out，或確認不要了再自己刪掉"
}
if ((Get-WslDistros) -contains $BuildDistro) {
    throw "WSL 已經有 $BuildDistro（上次打包中斷留下的）：確認不要了再執行 wsl --unregister $BuildDistro"
}
$drive = (Split-Path -Qualifier ([IO.Path]::GetFullPath($Out))).TrimEnd(':')
$freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
if ($freeGb -lt 40) { throw "$($drive): 只剩 $freeGb GB，離線包約 25 GB，打包過程還要暫存空間（至少 40 GB）" }

$commit = (git rev-parse --short HEAD).Trim()
if (git status --porcelain --untracked-files=no) { Note "注意：有還沒 commit 的修改，不會打包進去（打包的是 $commit）" }

$ollamaProgram = Split-Path (Resolve-Exe 'ollama') -Parent
$ollamaModels = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama\models' }
$hfHub = if ($env:HF_HUB_CACHE) { $env:HF_HUB_CACHE }
    elseif ($env:HF_HOME) { Join-Path $env:HF_HOME 'hub' }
    else { Join-Path $env:USERPROFILE '.cache\huggingface\hub' }
# Ortho2CAD 模型不進 Git：這個資料夾沒有就到主要工作區找（在 git worktree 裡打包時）
$mainRepo = Split-Path (Resolve-Path (git rev-parse --git-common-dir)).Path -Parent
$ortho2cad = @((Join-Path $Repo 'models\ortho2cad'), (Join-Path $mainRepo 'models\ortho2cad')) |
    Where-Object { Test-Path (Join-Path $_ 'mmproj-ortho2cad-f16.gguf') } | Select-Object -First 1
if (-not $ortho2cad) { throw '找不到 models\ortho2cad（先跑 pipelines\setup_ortho2cad.py）' }
# llama.cpp：和 start-services.ps1 同一套找法
$llamaServer = if ($env:LLAMA_SERVER) { $env:LLAMA_SERVER } else { Resolve-Exe 'llama-server' }
if (-not $llamaServer) {
    $llamaServer = Get-ChildItem (Join-Path $env:USERPROFILE 'tools\llama.cpp-*\llama-server.exe') -ErrorAction SilentlyContinue |
        Sort-Object FullName -Descending | Select-Object -First 1 -ExpandProperty FullName
}
if (-not $llamaServer -or -not (Get-ChildItem (Split-Path $llamaServer) -Filter 'ggml-cuda*.dll')) {
    throw '找不到 CUDA 版的 llama-server（docs\5070ti-host.md「第一次安裝」第 6 步）'
}
# Inno Setup：winget 預設裝在使用者資料夾，也可能裝在 Program Files 或自訂位置
if (-not $Iscc) {
    $Iscc = @((Resolve-Exe 'iscc'), (Join-Path $env:LOCALAPPDATA 'Programs\Inno Setup 6\ISCC.exe'),
        (Join-Path ${env:ProgramFiles(x86)} 'Inno Setup 6\ISCC.exe'), 'D:\tools\InnoSetup6\ISCC.exe') |
        Where-Object { $_ -and (Test-Path $_) } | Select-Object -First 1
}
if (-not $Iscc -or -not (Test-Path $Iscc)) { throw '找不到 Inno Setup 6 的 ISCC.exe（winget install JRSoftware.InnoSetup，或用 -Iscc 指定）' }
$javaHome = if ($env:JAVA_HOME) { $env:JAVA_HOME } else { Split-Path (Split-Path (Resolve-Exe 'java') -Parent) -Parent }
if (-not (Test-Path (Join-Path $javaHome 'bin\java.exe'))) { throw '找不到 Java 21（winget install Microsoft.OpenJDK.21）' }

Note "版本：$commit"
Note "Ollama：$ollamaProgram（模型 $ollamaModels）"
Note "Hugging Face 快取：$hfHub"
Note "Ortho2CAD：$ortho2cad；llama-server：$llamaServer"
Note "Java：$javaHome"
Note "Inno Setup：$Iscc"
Note "輸出：$Out（$($drive): 剩 $freeGb GB）"

# 安裝精靈把 payload\ 整個複製到安裝位置：裝好的結構是 app\、offline\、desktop\ 三個資料夾並排
$Payload = Join-Path $Out 'payload'
$App = Join-Path $Payload 'app'
$Offline = Join-Path $Payload 'offline'
$Tmp = Join-Path $Out 'tmp'
New-Item -ItemType Directory -Force $App, $Offline, $Tmp, (Join-Path $Offline 'wsl') | Out-Null

# ---------------------------------------------------------------- 1. 原始碼
Say "原始碼（git archive $commit）"
$zip = Join-Path $Tmp 'source.zip'
Run 'git' @('archive', '--format=zip', '-o', $zip, 'HEAD')
Expand-Archive -Path $zip -DestinationPath $App
Remove-Item $zip

# ---------------------------------------------------------------- 2. WSL 環境（取代 Docker）
# 從乾淨的 Ubuntu 建：和這台自己的 WSL 發行版完全分開，不會帶到個人檔案
Say 'WSL 環境：Ubuntu 24.04＋PostgreSQL／pgvector、Redis、CadQuery'
$rootfs = Join-Path $Tmp $RootfsName
Invoke-WebRequest -UseBasicParsing "$RootfsBase/$RootfsName" -OutFile $rootfs
# 伺服器沒給 Content-Type：PowerShell 5.1 的 .Content 會是位元組陣列，先存檔再讀成文字
$sumsFile = Join-Path $Tmp 'SHA256SUMS'
Invoke-WebRequest -UseBasicParsing "$RootfsBase/SHA256SUMS" -OutFile $sumsFile
$sums = Get-Content $sumsFile -Raw
$expect = ($sums -split "`n" | Where-Object { $_ -match [regex]::Escape($RootfsName) } | Select-Object -First 1) -split '\s+' | Select-Object -First 1
$actual = (Get-FileHash $rootfs -Algorithm SHA256).Hash.ToLower()
if (-not $expect -or $actual -ne $expect.ToLower()) { throw "Ubuntu 映像的 SHA-256 不符（$actual ≠ $expect）" }
Note '✓ SHA-256 驗證通過'
Wsl @('--import', $BuildDistro, (Join-Path $Tmp 'wsl-build'), $rootfs, '--version', '2')
try {
    Wsl @('-d', $BuildDistro, '-u', 'root', '--exec', '/bin/bash', (ConvertTo-WslPath (Join-Path $App 'deploy\offline\wsl-setup.sh')))
    Wsl @('--terminate', $BuildDistro)
    Wsl @('--export', $BuildDistro, (Join-Path $Offline 'wsl\artrag.tar'))
} finally {
    # 暫時的建置環境：匯出之後就不需要了（它是這支腳本自己 import 的）
    $prev = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    wsl.exe --unregister $BuildDistro *> $null
    $ErrorActionPreference = $prev
}
Remove-Item $rootfs

# 沒有 WSL 的電腦用：WSL 的離線安裝程式（GitHub 上 microsoft/WSL 最新的正式版）
$release = Invoke-RestMethod -UseBasicParsing 'https://api.github.com/repos/microsoft/WSL/releases/latest'
$msi = $release.assets | Where-Object { $_.name -like '*x64.msi' } | Select-Object -First 1
if (-not $msi) { throw "microsoft/WSL $($release.tag_name) 找不到 x64.msi" }
Invoke-WebRequest -UseBasicParsing $msi.browser_download_url -OutFile (Join-Path $Offline "wsl\$($msi.name)")
Note "✓ WSL 離線安裝程式：$($msi.name)"

# ---------------------------------------------------------------- 3. 前端
# 裝的電腦不用 Node.js：這裡建置好，node_modules 不帶走
Say '建置前端'
Push-Location (Join-Path $App 'frontend')
try { Run 'npm' @('ci'); Run 'npm' @('run', 'build') } finally { Pop-Location }
Remove-Item -Recurse -Force (Join-Path $App 'frontend\node_modules')

# ---------------------------------------------------------------- 3b. 桌面程式（Electron）
Say '建置桌面程式 art-rag.exe'
$desktopSrc = Join-Path $App 'desktop'
Push-Location $desktopSrc
try { Run 'npm' @('ci'); Run 'npm' @('run', 'pack') } finally { Pop-Location }
Move-Item (Join-Path $desktopSrc 'dist\art-rag-win32-x64') (Join-Path $Payload 'desktop')
foreach ($leftover in 'node_modules', 'dist') { Remove-Item -Recurse -Force (Join-Path $desktopSrc $leftover) }

# ---------------------------------------------------------------- 4. 排程服務（Timefold）與 Java
Say '排程服務（Maven 建置）與 Java 21'
$env:JAVA_HOME = $javaHome
Run 'uv' @('run', '--no-project', '--with', 'httpx', 'python', (Join-Path $App 'pipelines\setup_scheduler.py'), '--skip-tests')
# 裝的電腦只要 scheduler.jar；.java-home 安裝時改成離線包的 Java
Get-ChildItem (Join-Path $App 'scheduler\target') | Where-Object { $_.Name -ne 'scheduler.jar' } | Remove-Item -Recurse -Force
foreach ($leftover in '.maven', '.java-home') {
    $p = Join-Path $App "scheduler\$leftover"
    if (Test-Path $p) { Remove-Item -Recurse -Force $p }
}
Copy-Item -Recurse $javaHome (Join-Path $Offline 'jdk')

# ---------------------------------------------------------------- 5. Python 與套件
# 虛擬環境不能搬（裡面寫死路徑）：把 Python 與 uv.lock 的套件放進離線包自己的快取，裝的電腦用快取重建
Say 'Python 3.12 與套件（照 backend\uv.lock）'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Offline 'python'
$env:UV_CACHE_DIR = Join-Path $Offline 'uv-cache'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Tmp 'venv'
Run 'uv' @('python', 'install', '3.12')
Push-Location (Join-Path $App 'backend')
try { Run 'uv' @('sync', '--frozen') } finally { Pop-Location }
Remove-Item -Recurse -Force $env:UV_PROJECT_ENVIRONMENT
Remove-Item Env:UV_PROJECT_ENVIRONMENT

New-Item -ItemType Directory -Force (Join-Path $Offline 'uv') | Out-Null
foreach ($name in 'uv', 'uvx') {
    $exe = Resolve-Exe $name
    if ($exe) { Copy-Item $exe (Join-Path $Offline "uv\$name.exe") }
}

# ---------------------------------------------------------------- 6. 模型與 Ollama、Ortho2CAD
Say '模型與 Ollama（從這台複製）'
$bundleArgs = @('run', '--no-project', 'python', (Join-Path $App 'deploy\offline\bundle.py'),
    '--out', $Offline, '--models-yaml', (Join-Path $App 'shared\models.yaml'),
    '--ollama-models', $ollamaModels, '--ollama-program', $ollamaProgram, '--hf-hub', $hfHub)
foreach ($m in $Models) { $bundleArgs += @('--model', $m) }
Run 'uv' $bundleArgs

Say 'Ortho2CAD 模型與 llama.cpp（CUDA 版）'
# start-services.ps1、deploy\llama-router.ini 都從 app\models\ortho2cad 讀
$ortho2cadOut = Join-Path $App 'models\ortho2cad'
New-Item -ItemType Directory -Force $ortho2cadOut | Out-Null
Get-ChildItem $ortho2cad -Filter '*.gguf' | Copy-Item -Destination $ortho2cadOut
Copy-Item -Recurse (Split-Path $llamaServer -Parent) (Join-Path $Offline 'llama.cpp')

# ---------------------------------------------------------------- 7. 完成標記、安裝精靈
Copy-Item (Join-Path $App 'deploy\offline\使用說明.txt') (Join-Path $Out '使用說明.txt')
Remove-Item -Recurse -Force $Tmp
# offline.json 最後才寫：有它才算完整的離線包（offline-env.ps1 靠它判斷）
$info = [ordered]@{
    commit = $commit
    created = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    models = $Models
    ollama_port = 11435
    wsl_distro = 'artrag'
    wsl_installer = $msi.name
}
[IO.File]::WriteAllText((Join-Path $Offline 'offline.json'), ($info | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

Say '安裝精靈 setup.exe（Inno Setup）'
$payloadBytes = (Get-ChildItem $Payload -Recurse -File -Force | Measure-Object Length -Sum).Sum
# 安裝後再用約 10 GB（Python 環境、WSL 環境、索引）：一起算進安裝精靈要求的空間
$isccArgs = @('/Q', "/DAppVersion=$((Get-Date).ToString('yyyy.M.d'))", "/DCommit=$commit",
    "/DExtraBytes=$([int64]$payloadBytes + 10GB)", "/DOutputDir=$Out", (Join-Path $App 'deploy\offline\art-rag.iss'))
Run $Iscc $isccArgs

$sizeGb = [math]::Round((Get-ChildItem $Out -Recurse -File -Force | Measure-Object Length -Sum).Sum / 1GB, 1)
Say "完成：$Out（$sizeGb GB）"
Note '把整個資料夾複製到別台電腦（隨身碟要 exFAT 或 NTFS），點兩下 setup.exe'
