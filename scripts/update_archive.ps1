# tombkeeper 微博更新脚本（默认增量，定时任务入口）
#
# 用法:
#   powershell -ExecutionPolicy Bypass -File scripts\update_archive.ps1            # 增量更新
#   powershell -ExecutionPolicy Bypass -File scripts\update_archive.ps1 -Full      # 全量抓取(无存档)/断点续跑(有存档)
#
# 流程:
#   1. 抓取微博（默认增量：读存档最新 note_id，翻到边界即停，分钟级；-Full 则全量/续跑）
#   2. 生成单文件 HTML 时间线（新图增量下载到本地）
#   3. 日志落盘 logs\（archive_*.log 摘要 + crawl_*.log 详情）
#
# 注意:
#   - CDP 模式需要桌面会话，任务计划程序请设"只在用户登录时运行"
#   - 若微博登录态过期，抓取会进入扫码流程并超时退出；届时手动跑一次扫码即可恢复
param([switch]$Full)

$ErrorActionPreference = "Stop"
try {
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
    Write-Host "[ERROR] 未找到 $py" -ForegroundColor Red
    exit 1
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("archive_{0}.log" -f $stamp)
$detailFile = Join-Path $logDir ("crawl_{0}.log" -f $stamp)

function Write-Log($msg) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $msg
    Write-Host $line
    Add-Content -Path $logFile -Value $line -Encoding UTF8
}

function Invoke-Step($name, [scriptblock]$body) {
    Write-Log "===== $name ====="
    try {
        & $body
        Write-Log "----- $name 完成 -----"
    } catch {
        Write-Log "[ERROR] $name 失败: $($_.Exception.Message)"
        exit 1
    }
}

function Show-Tail {
    Start-Sleep -Milliseconds 500
    try {
        Get-Content -Path $detailFile -Tail 6 -Encoding UTF8 -ErrorAction SilentlyContinue | ForEach-Object { Write-Log "  $_" }
    } catch { }
}

Write-Log "===== tombkeeper 微博存档 更新开始 ====="

$before = [int]((& $py tools\weibo_count.py 2>$null).Trim())
Write-Log "更新前存档条数: $before"

Invoke-Step "1/2 抓取微博" {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    if ($Full) { $env:WEIBO_FULL_CRAWL = "1"; Write-Log "全量/断点续跑模式" } else { Remove-Item Env:WEIBO_FULL_CRAWL -ErrorAction SilentlyContinue }
    & cmd /c "`"$py`" main.py --platform wb --lt qrcode --type creator >>`"$detailFile`" 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "抓取失败 (exit=$LASTEXITCODE)" }
    $sw.Stop()
    Write-Log "抓取耗时: $([math]::Round($sw.Elapsed.TotalMinutes,1)) 分钟"
}

$after = [int]((& $py tools\weibo_count.py 2>$null).Trim())

Invoke-Step "2/2 生成 HTML 时间线" {
    & cmd /c "`"$py`" tools\weibo_jsonl_to_html.py --input data/weibo/jsonl/creator_contents_*.jsonl --output data/weibo/tombkeeper_weibo.html --download-images --workers 6 >>`"$detailFile`" 2>&1"
    if ($LASTEXITCODE -ne 0) { throw "生成失败 (exit=$LASTEXITCODE)" }
}

Show-Tail

$delta = $after - $before
Write-Log "===== 完成: 存档 $after 条 (本次新增 $delta) | HTML: data\weibo\tombkeeper_weibo.html | 详情日志: $detailFile ====="
