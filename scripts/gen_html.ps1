# 重新生成 tombkeeper HTML 归档（从现有 jsonl，增量下载图片）
# 用法: powershell -ExecutionPolicy Bypass -File scripts\gen_html.ps1
$ErrorActionPreference = "Stop"
try {
    $OutputEncoding = [System.Text.Encoding]::UTF8
    [Console]::OutputEncoding = [System.Text.Encoding]::UTF8
} catch { }

$root = Split-Path -Parent $PSScriptRoot
Set-Location $root

$py = Join-Path $root ".venv\Scripts\python.exe"
$logDir = Join-Path $root "logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logFile = Join-Path $logDir ("htmlgen_{0}.log" -f (Get-Date -Format "yyyyMMdd_HHmmss"))

& $py tools\weibo_jsonl_to_html.py `
    --input data\weibo\jsonl\creator_contents_*.jsonl `
    --output data\weibo\tombkeeper_weibo.html `
    --download-images --workers 6 *>&1 | Tee-Object -FilePath $logFile

if ($LASTEXITCODE -ne 0) {
    Write-Host "[ERROR] HTML 生成失败 exit=$LASTEXITCODE 日志=$logFile"
    exit 1
}
Write-Host "HTML 生成完成: data\weibo\tombkeeper_weibo.html | 日志=$logFile"
