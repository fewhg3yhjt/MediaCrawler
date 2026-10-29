# -*- coding: utf-8 -*-
"""每日自动化编排脚本（云上 cron 用）。用法:
    python scripts/run_daily.py            # 增量模式（默认，每日定时）
    python scripts/run_daily.py --full     # 全量模式（首次部署手动跑）

流程（按顺序，单实例锁，任一失败继续下一个并记录）:
    incremental: sync_follows_fans -> batch_crawl_tracked -> creator 增量
    full:        sync_follows_fans -> supervise_self_export(all) -> creator 全量 -> 跟踪用户全量
"""
import argparse
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
LOG_DIR = ROOT / "logs"
LOCK_FILE = LOG_DIR / "run_daily.lock"
STALL_LOG_MAX_AGE = 6 * 3600  # 锁文件超过该时间视为残留


def venv_python() -> str:
    if sys.platform == "win32":
        return str(ROOT / ".venv" / "Scripts" / "python.exe")
    return str(ROOT / ".venv" / "bin" / "python")


def acquire_lock() -> bool:
    """单实例锁：有存活实例则退出，防止 cron 与手动重复启动。"""
    LOG_DIR.mkdir(exist_ok=True)
    if LOCK_FILE.exists():
        stale = (time.time() - LOCK_FILE.stat().st_mtime) > STALL_LOG_MAX_AGE
        try:
            pid = int(LOCK_FILE.read_text().strip())
            alive = os.path.exists(f"/proc/{pid}") if sys.platform != "win32" else f"pid:{pid}" != ""
        except Exception:
            alive = False
        if alive and not stale:
            print(f"[run_daily] 已有实例运行 (PID {pid})，本次退出")
            return False
    LOCK_FILE.write_text(str(os.getpid()))
    return True


def release_lock():
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def log(msg: str):
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}"
    print(line, flush=True)


def run_stage(name: str, cmd: list, timeout: int):
    """顺序执行一个阶段，超时/失败只记录不中断整链。"""
    log(f"==> 阶段: {name}")
    logf = LOG_DIR / f"daily_{name}.log"
    try:
        proc = subprocess.run(
            cmd, cwd=str(ROOT),
            stdout=open(logf, "wb"), stderr=subprocess.STDOUT,
            timeout=timeout,
        )
        ok = proc.returncode == 0
        log(f"    完成 {name} exit={proc.returncode} (日志: {logf.name})")
    except subprocess.TimeoutExpired:
        log(f"    {name} 超时(>{timeout}s)，已跳过")
    except Exception as e:
        log(f"    {name} 失败: {e}")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("--full", action="store_true", help="全量模式")
    args = parser.parse_args()
    py = venv_python()
    LOG_DIR.mkdir(exist_ok=True)
    if not acquire_lock():
        return
    try:
        if args.full:
            # 首日全量：收藏/赞过（看门狗自动重启）+ creator 全量 + 跟踪用户全量
            run_stage("sync_follows_fans", [py, "scripts/sync_follows_fans.py"], timeout=3600 * 2)
            run_stage("self_export", [py, "scripts/supervise_self_export.py", "all"], timeout=3600 * 24)
            env = dict(os.environ)
            env["WEIBO_FULL_CRAWL"] = "1"
            run_stage("creator_full", [py, "main.py", "--platform", "wb", "--lt", "qrcode",
                                       "--type", "creator", "--save_data_option", "jsonl"],
                      timeout=3600 * 12)
            # 跟踪用户全量：需先在关系库把 crawl_mode 设为 full（运行前人工确认）
            run_stage("batch_crawl_tracked", [py, "scripts/batch_crawl_tracked.py"], timeout=3600 * 12)
        else:
            # 每日增量
            run_stage("sync_follows_fans", [py, "scripts/sync_follows_fans.py"], timeout=3600 * 2)
            run_stage("batch_crawl_tracked", [py, "scripts/batch_crawl_tracked.py"], timeout=3600 * 8)
            run_stage("creator_incremental", [py, "main.py", "--platform", "wb", "--lt", "qrcode",
                                              "--type", "creator", "--save_data_option", "jsonl"],
                      timeout=3600 * 6)
        log("==> 本轮结束")
    finally:
        release_lock()


if __name__ == "__main__":
    main()