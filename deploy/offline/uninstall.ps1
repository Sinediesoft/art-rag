<#
解除安裝前的清理（docs/adr/047）：setup.exe 裝的 art-rag 從「新增或移除程式」移除時由解除安裝程式呼叫，之後它會刪掉整個安裝資料夾。
關掉桌面程式 → 停止全部服務 → 移除這次安裝匯入的 WSL 環境 artrag（虛擬磁碟在安裝資料夾裡才移除，別處匯入的不動）。
-StopOnly：只關程式、停服務（setup.exe 覆蓋安裝新版之前用，WSL 環境與資料留著）。
#>
param([switch]$StopOnly)

$ErrorActionPreference = 'Continue'
$Repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Root = Split-Path $Repo -Parent
. (Join-Path $Repo 'deploy\offline\offline-env.ps1')

# 桌面程式一關，它起的服務（同一個 Job）也會跟著結束；剩下的由 -Stop 收
Get-Process -Name 'art-rag' -ErrorAction SilentlyContinue |
    Where-Object { $_.Path -and $_.Path.StartsWith($Root, [StringComparison]::OrdinalIgnoreCase) } |
    Stop-Process -Force
& (Join-Path $Repo 'deploy\windows\start-services.ps1') -Stop -NoDiagnose
if ($StopOnly) { return }

$lxss = 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Lxss'
$distro = Get-ChildItem $lxss -ErrorAction SilentlyContinue | Get-ItemProperty |
    Where-Object { $_.DistributionName -eq $OfflineWslDistro } | Select-Object -First 1
if ($distro -and $distro.BasePath -and
    ($distro.BasePath -replace '^\\\\\?\\', '').StartsWith($Root, [StringComparison]::OrdinalIgnoreCase)) {
    wsl.exe --unregister $OfflineWslDistro *> $null
    "已移除 WSL 環境 $OfflineWslDistro"
} elseif ($distro) {
    "WSL 環境 $OfflineWslDistro 不在這個安裝資料夾（$($distro.BasePath)），不移除"
}
