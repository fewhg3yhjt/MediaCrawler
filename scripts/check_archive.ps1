# 微博全量抓取进度检查脚本
# - 输出并记录: 时间 | 存档条数 | 距上次新增 | 速率(条/分) | 预计剩余(分) | 进程存活
# - 追加写入 logs\progress_history.log（供长时间观察进度历史）
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\check_archive.ps1
param([int]$Target = 25087)

$ErrorActionPreference = "Stop"
try {
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
$count = [int]((& $py tools\weibo_count.py 2>$null).Trim())
$now = Get-Date -Format "yyyy-MM-dd HH:mm:ss"

# 读取上一次快照（用于算速率/ETA）
$history = Join-Path $root "logs\progress_history.log"
$prev = $null
if (Test-Path $history) {
    $lastLine = (Get-Content -Path $history -Encoding UTF8 -Tail 1 -ErrorAction SilentlyContinue | Select-Object -Last 1)
    if ($lastLine) {
        $parts = $lastLine.Split("|")
        if ($parts.Count -ge 3) {
            try {
                $prevCount = [int]$parts[1].Trim()
                $prevTime = [datetime]::ParseExact($parts[0].Trim(), "yyyy-MM-dd HH:mm:ss", $null)
                $prev = @{ count = $prevCount; time = $prevTime }
            } catch { $prev = $null }
        }
    }
}

$delta = 0; $rate = 0; $eta = -1
if ($prev) {
    $delta = $count - $prev.count
    $mins = (Get-Date) - $prev.time
    $minsF = [math]::Max($mins.TotalMinutes, 0.01)
    $rate = [math]::Round($delta / $minsF, 1)
    if ($rate -gt 0) { $eta = [math]::Round(($Target - $count) / $rate) }
}
$remaining = $Target - $count
if ($remaining -lt 0) { $remaining = 0 }

# 进程是否存活
$alive = "no"
$proc = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue
if ($proc | Where-Object { $_.CommandLine -match "main.py" }) { $alive = "yes" }

$line = "$now | $count | $delta | $rate | $eta | $alive"
Add-Content -Path $history -Value $line -Encoding UTF8

Write-Host "存档条数: $count / $Target (完成 $([math]::Round($count * 100.0 / $Target, 1))%)"
Write-Host "距上次快照新增: $delta | 速率: $rate 条/分 | 预计剩余: $eta 分钟"
Write-Host "抓取进程存活: $alive"
Write-Host "进度历史: $history"
