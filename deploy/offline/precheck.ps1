<#
安裝前的硬體與環境檢查（docs/adr/047）：setup.exe 的第二頁呼叫，結果寫成文字檔給安裝精靈顯示。
只讀不改：不需要系統管理員權限，也不啟用任何功能（真正的安裝在第一次打開萊問時做）。
顯卡選方案的門檻和 install-offline.ps1 一樣：12 GB 以上 8B、4～12 GB 4B、沒有 NVIDIA 顯卡只能看介面。

  precheck.ps1 -Out C:\Temp\precheck.txt [-NeedGb 36]
第一行是摘要（OK 或 WARN），之後每行是一個項目。
#>
param(
    [Parameter(Mandatory = $true)][string]$Out,
    [int]$NeedGb = 36
)

$ErrorActionPreference = 'Continue'
$lines = New-Object System.Collections.Generic.List[string]
$warn = 0
function Item([string]$mark, [string]$text) {
    if ($mark -ne '✓') { $script:warn++ }
    $lines.Add("$mark $text")
}

# Windows：WSL2 的 localhost 轉送與離線的 WSL 安裝檔要 21H2（19044）以上
$os = Get-CimInstance Win32_OperatingSystem
$build = [int]$os.BuildNumber
if ($build -ge 19044) { Item '✓' "作業系統：$($os.Caption)（組建 $build）" }
else { Item '✗' "作業系統：$($os.Caption)（組建 $build）：需要 Windows 10 21H2（19044）以上或 Windows 11" }

# 記憶體：8B 模型、索引、WSL 環境同時在跑
$cs = Get-CimInstance Win32_ComputerSystem
$ramGb = [math]::Round($cs.TotalPhysicalMemory / 1GB)
if ($ramGb -ge 16) { Item '✓' "記憶體：$ramGb GB" }
else { Item '⚠' "記憶體：$ramGb GB：建議 16 GB 以上，少於這個數字回答會比較慢" }

# 顯卡：nvidia-smi 有裝驅動程式才有
$vramMb = 0
$gpuName = $null
if (Get-Command nvidia-smi -ErrorAction SilentlyContinue) {
    $line = (& nvidia-smi --query-gpu=name,memory.total --format=csv,noheader,nounits 2>$null | Select-Object -First 1)
    if ($line) {
        $parts = $line -split ','
        $gpuName = $parts[0].Trim()
        $vramMb = [int]$parts[1].Trim()
    }
}
$vramGb = [math]::Round($vramMb / 1024, 1)
if ($vramMb -ge 12000) { Item '✓' "顯示卡：$gpuName（$vramGb GB）→ 用 Qwen3-VL 8B 模型" }
elseif ($vramMb -ge 4000) { Item '✓' "顯示卡：$gpuName（$vramGb GB）→ 用 Qwen3-VL 4B 模型（12 GB 以上才用 8B）" }
elseif ($gpuName) { Item '⚠' "顯示卡：$gpuName（$vramGb GB）：不到 4 GB，只能看介面與流程，不會產生回答" }
else {
    $other = (Get-CimInstance Win32_VideoController | Select-Object -First 1).Name
    Item '⚠' "顯示卡：沒有偵測到 NVIDIA 顯示卡（$other）：只能看介面與流程，不會產生回答，3D 重建很慢"
}

# 虛擬化與 WSL2：資料庫、Redis、3D 重建的沙箱都在 WSL2 裡
$cpu = Get-CimInstance Win32_Processor | Select-Object -First 1
# Hyper-V 已經在跑時 VirtualizationFirmwareEnabled 會回 False，要一起看 HypervisorPresent
$virtOk = $cs.HypervisorPresent -or $cpu.VirtualizationFirmwareEnabled
if (-not $virtOk) {
    Item '✗' 'BIOS 的虛擬化功能沒有開：WSL2 需要它。請重開機進 BIOS 開啟 Intel VT-x 或 AMD SVM（可以先裝，開好再打開萊問）'
}
else {
    & wsl.exe --status *> $null
    if ($LASTEXITCODE -eq 0) { Item '✓' 'WSL2：已經可以用' }
    else { $lines.Add('• WSL2：還沒啟用。第一次打開萊問時會自動啟用（跳出系統管理員確認時按「是」，完成後重新開機一次）') }
}

# 磁碟：安裝位置要 $NeedGb GB（複製約 26 GB，第一次打開時再用約 10 GB）
$drives = Get-CimInstance Win32_LogicalDisk -Filter 'DriveType=3' | Sort-Object DeviceID
$okDrives = @($drives | Where-Object { $_.FreeSpace / 1GB -ge $NeedGb } | ForEach-Object { $_.DeviceID })
$free = ($drives | ForEach-Object { "$($_.DeviceID) 剩 $([math]::Round($_.FreeSpace / 1GB)) GB" }) -join '、'
if ($okDrives) { Item '✓' "磁碟空間：$free（需要 $NeedGb GB，下一步選安裝位置時請選 $($okDrives -join ' 或 ')）" }
else { Item '✗' "磁碟空間：$free：需要一顆磁碟有 $NeedGb GB 可用空間" }

$summary = if ($warn -eq 0) { 'OK 這台電腦可以安裝萊問。' } else { "WARN 有 $warn 個項目要注意，可以繼續安裝，但請先看下面的說明。" }
$lines.Insert(0, $summary)
[IO.File]::WriteAllLines($Out, $lines, (New-Object Text.UTF8Encoding $true))
