<#
做離線安裝包（docs/adr/046）：在已經裝好、可以連網的電腦（5070 Ti 主機）跑一次，輸出一個資料夾。
把整個資料夾複製到別台 Windows 電腦後，點兩下「安裝.cmd」就能裝好並啟動，安裝過程不連網。

  powershell -ExecutionPolicy Bypass -File deploy\offline\pack.ps1                         # 輸出到 D:\art-rag-offline
  powershell -ExecutionPolicy Bypass -File deploy\offline\pack.ps1 -Out E:\art-rag-offline

- 打包時會連網：可攜版 Python 3.12、Python 套件（照 backend\uv.lock）、前端套件（npm ci）
- 模型直接從這台複製：Ollama 的 qwen3-vl 8B／4B，Hugging Face 快取裡 shared\models.yaml 釘選的 bge-m3、Chinese-CLIP
- 只打包已 commit 的內容（git archive HEAD）：.env、data\ 不會帶出去
- 輸出約 17 GB；複製到隨身碟要用 exFAT 或 NTFS（8B 模型單一個檔案超過 4 GB，FAT32 放不下）
- 不含 Docker（PostgreSQL、Redis）、Ortho2CAD、Java 排程：離線包用 SQLite，3D 重建與排程的背景工作不能用
#>
param(
    [string]$Out = 'D:\art-rag-offline',
    [string[]]$Models = @('qwen3-vl:8b-instruct', 'qwen3-vl:4b-instruct')
)

$ErrorActionPreference = 'Stop'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
Set-Location $Repo

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

# ---------------------------------------------------------------- 0. 檢查
Say '檢查這台電腦'
foreach ($cmd in 'git', 'uv', 'npm', 'ollama') {
    if (-not (Get-Command $cmd -ErrorAction SilentlyContinue)) { throw "找不到 $cmd：打包要在已經裝好整套系統的電腦上跑" }
}
if ((Test-Path $Out) -and (Get-ChildItem $Out -Force | Select-Object -First 1)) {
    throw "$Out 已經有東西：換一個 -Out，或確認不要了再自己刪掉"
}
$drive = (Split-Path -Qualifier ([IO.Path]::GetFullPath($Out))).TrimEnd(':')
$freeGb = [math]::Round((Get-PSDrive $drive).Free / 1GB, 1)
if ($freeGb -lt 25) { throw "$($drive): 只剩 $freeGb GB，離線包約 17 GB，打包過程還要暫存空間（至少 25 GB）" }

$commit = (git rev-parse --short HEAD).Trim()
if (git status --porcelain --untracked-files=no) { Note "注意：有還沒 commit 的修改，不會打包進去（打包的是 $commit）" }

$ollamaProgram = Split-Path (Resolve-Exe 'ollama') -Parent
$ollamaModels = if ($env:OLLAMA_MODELS) { $env:OLLAMA_MODELS } else { Join-Path $env:USERPROFILE '.ollama\models' }
$hfHub = if ($env:HF_HUB_CACHE) { $env:HF_HUB_CACHE }
    elseif ($env:HF_HOME) { Join-Path $env:HF_HOME 'hub' }
    else { Join-Path $env:USERPROFILE '.cache\huggingface\hub' }
Note "版本：$commit"
Note "Ollama：$ollamaProgram（模型 $ollamaModels）"
Note "Hugging Face 快取：$hfHub"
Note "輸出：$Out（$($drive): 剩 $freeGb GB）"

$App = Join-Path $Out 'app'
$Offline = Join-Path $Out 'offline'
New-Item -ItemType Directory -Force $App, $Offline | Out-Null

# ---------------------------------------------------------------- 1. 原始碼
Say "原始碼（git archive $commit）"
$zip = Join-Path $Out 'source.zip'
Run 'git' @('archive', '--format=zip', '-o', $zip, 'HEAD')
Expand-Archive -Path $zip -DestinationPath $App
Remove-Item $zip

# ---------------------------------------------------------------- 2. 前端
# 裝的電腦不用 Node.js：這裡建置好，node_modules 不帶走
Say '建置前端'
Push-Location (Join-Path $App 'frontend')
try { Run 'npm' @('ci'); Run 'npm' @('run', 'build') } finally { Pop-Location }
Remove-Item -Recurse -Force (Join-Path $App 'frontend\node_modules')

# ---------------------------------------------------------------- 3. Python 與套件
# 虛擬環境不能搬（裡面寫死路徑）：把 Python 與 uv.lock 的套件放進離線包自己的快取，裝的電腦用快取重建
Say 'Python 3.12 與套件（照 backend\uv.lock）'
$env:UV_PYTHON_INSTALL_DIR = Join-Path $Offline 'python'
$env:UV_CACHE_DIR = Join-Path $Offline 'uv-cache'
$env:UV_PYTHON_PREFERENCE = 'only-managed'
$env:UV_PROJECT_ENVIRONMENT = Join-Path $Out 'tmp-venv'
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

# ---------------------------------------------------------------- 4. 模型與 Ollama
Say '模型與 Ollama（從這台複製）'
$bundleArgs = @('run', '--no-project', 'python', (Join-Path $App 'deploy\offline\bundle.py'),
    '--out', $Offline, '--models-yaml', (Join-Path $App 'shared\models.yaml'),
    '--ollama-models', $ollamaModels, '--ollama-program', $ollamaProgram, '--hf-hub', $hfHub)
foreach ($m in $Models) { $bundleArgs += @('--model', $m) }
Run 'uv' $bundleArgs

# ---------------------------------------------------------------- 5. 點兩下用的檔案、完成標記
Say '安裝、啟動、停止的捷徑檔'
foreach ($name in '安裝.cmd', '啟動.cmd', '停止.cmd', '使用說明.txt') {
    Copy-Item (Join-Path $App "deploy\offline\$name") (Join-Path $Out $name)
}
# offline.json 最後才寫：有它才算完整的離線包（offline-env.ps1 靠它判斷）
$info = [ordered]@{
    commit = $commit
    created = (Get-Date).ToString('yyyy-MM-ddTHH:mm:ssK')
    models = $Models
    ollama_port = 11435
}
[IO.File]::WriteAllText((Join-Path $Offline 'offline.json'), ($info | ConvertTo-Json), (New-Object Text.UTF8Encoding $false))

$sizeGb = [math]::Round((Get-ChildItem $Out -Recurse -File -Force | Measure-Object Length -Sum).Sum / 1GB, 1)
Say "完成：$Out（$sizeGb GB）"
Note '把整個資料夾複製到別台電腦的本機磁碟（不要直接在隨身碟上裝），點兩下「安裝.cmd」'
