#!/usr/bin/env bash
# =============================================================================
# 华为云 Ubuntu 一键环境初始化 + 项目部署（media_crawler-weibo 定制版）
#
# 用法:
#   bash deploy/setup_cloud.sh   # 在本机 clone 好的项目目录内执行
#
# 说明:
#   - 需要 root 或 sudo 权限
#   - 仅安装系统级依赖，不 clone（假定你已 clone 项目到这里）
#   - 运行后需手动确认: ① 装浏览器依赖 ② 首次扫码 ③ 启动服务
# =============================================================================
set -euo pipefail

echo "==> [1/6] 更新 apt 并安装基础依赖"
apt-get update -y
apt-get install -y --no-install-recommends \
    python3 python3-venv python3-pip curl wget git unzip zip \
    ca-certificates fonts-liberation libasound2 libatk-bridge2.0-0 \
    libatk1.0-0 libcups2 libdbus-1-3 libdrm2 libxcomposite1 \
    libxdamage1 libxfixes3 libxkbcommon0 libxrandr2 libgbm1 \
    libnss3 libpango-1.0-0 fonts-noto-color-emoji libxshmfence1 \
    cron logrotate

echo "==> [2/6] 安装 uv（Python 包管理器）"
if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # 将 uv 加入当前与后续 shell 的 PATH
    export PATH="$HOME/.local/bin:$PATH"
    grep -q '~/.local/bin' "$HOME/.bashrc" || echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
fi

echo "==> [3/6] 创建项目虚拟环境并安装依赖"
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
if [ ! -d .venv ]; then
    uv venv --python "$(cat .python-version 2>/dev/null || echo 3.10)" .venv
fi
uv pip install --python .venv/bin/python -r requirements.txt
uv pip install --python .venv/bin/python playwright

echo "==> [4/6] 安装 Playwright 浏览器 (chromium) + 系统依赖"
.venv/bin/playwright install chromium
.venv/bin/playwright install-deps chromium || echo "  (install-deps 部分失败，可后续手动执行)"

echo "==> [5/6] 准备数据/日志目录"
mkdir -p data/weibo/jsonl data/weibo/images data/weibo/exports logs
chmod 755 data logs

echo "==> [6/6] 配置定时自动化 (cron)"
CRON_CMD="0 3 * * * cd $ROOT && .venv/bin/python scripts/run_daily.py >> $ROOT/logs/cron_daily.log 2>&1"
( crontab -l 2>/dev/null | grep -v "scripts/run_daily.py" ; echo "$CRON_CMD" ) | crontab -
echo "    已写入每日定时任务（每日 03:00）"
crontab -l | grep run_daily || true

echo
echo "======================================================================"
echo " 部署完成！接下来 3 步（手动）:"
echo " 1) 确认 chromium 可运行:  .venv/bin/playwright install-deps chromium"
echo " 2) 首次扫码登录（需有显示环境，或先在本机扫码导出 cookie）："
echo "       .venv/bin/python main.py --platform wb --lt qrcode --type creator"
echo " 3) 启动归档前台:  nohup .venv/bin/python -m weibo_web.main > logs/web.log 2>&1 &"
echo "    （可选）启动 WebUI:  .venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8080"
echo "======================================================================"
