# -*- coding: utf-8 -*-
"""收藏/点赞导出 supervisor：带看门狗，卡死自动重启，断点续传，直到抓完。
用法: python scripts/supervise_self_export.py [favorite|liked|all]
"""
import glob
import json
import os
import pathlib
import subprocess
import sys
import time
import datetime

ROOT = pathlib.Path(__file__).resolve().parent.parent
if sys.platform == "win32":
    PY = ROOT / ".venv" / "Scripts" / "python.exe"
else:
    PY = ROOT / ".venv" / "bin" / "python"
LOG_DIR = ROOT / "logs"
STALL_SECONDS = 180  # 日志超过该时间无更新视为卡死
MAX_RESTARTS = 60
LOCK_FILE = ROOT / "logs" / "supervise.lock"


def _pid_alive(pid):
    """检查进程是否存活（跨平台）。"""
    try:
        if sys.platform == "win32":
            proc = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-Process -Id %d -ErrorAction SilentlyContinue | Select-Object -ExpandProperty Id" % pid],
                capture_output=True, timeout=10,
            )
            return bool(proc.stdout.strip())
        os.kill(int(pid), 0)
        return True
    except Exception:
        return False


def acquire_lock():
    """单实例锁：已有存活实例则退出，防止重复启动导致资源耗尽。"""
    if LOCK_FILE.exists():
        try:
            pid = int(LOCK_FILE.read_text().strip())
            if _pid_alive(pid):
                print("[supervisor] 已有实例运行 (PID %d)，本次退出" % pid)
                sys.exit(0)
        except Exception:
            pass
    LOCK_FILE.write_text(str(os.getpid()))
    print("[supervisor] 获取单实例锁 PID %d" % os.getpid())


def log(msg):
    line = "[%s] %s" % (datetime.datetime.now().strftime("%H:%M:%S"), msg)
    print(line)
    with open(LOG_DIR / "supervise.log", "a", encoding="utf-8") as f:
        f.write(line + "\n")


def count_saved(prefix):
    ids = set()
    for p in glob.glob(str(ROOT / ("data/weibo/jsonl/%s_contents_*.jsonl" % prefix))):
        try:
            for line in open(p, encoding="utf-8"):
                line = line.strip()
                if line:
                    try:
                        ids.add(json.loads(line).get("note_id"))
                    except Exception:
                        pass
        except Exception:
            continue
    return len(ids)


def kill_cdp_chrome():
    """杀掉本工具拉起的 CDP Chrome（带 cdp_wb_user_data_dir 或 remote-debugging-port）。"""
    try:
        import subprocess as sp
        if sys.platform == "win32":
            sp.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | Where-Object { $_.CommandLine -match 'cdp_wb_user_data_dir|remote-debugging-port' } | ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=30,
            )
        else:
            for pattern in ("cdp_wb_user_data_dir", "remote-debugging-port"):
                sp.run(["pkill", "-f", pattern], capture_output=True, timeout=30)
    except Exception:
        pass


def run_once(feed_type, run_log):
    """启动一次爬虫；看门狗监控日志 mtime，卡死则杀掉。返回 (saved_before, saved_after, was_stalled, saw_done)"""
    before = count_saved(feed_type)
    log("启动 %s 导出 (已存 %s)..." % (feed_type, before))
    run_log.unlink(missing_ok=True)
    proc = subprocess.Popen(
        [str(PY), "main.py", "--platform", "wb", "--lt", "qrcode", "--type", feed_type, "--save_data_option", "jsonl"],
        cwd=str(ROOT),
        stdout=open(run_log, "wb"),
        stderr=subprocess.STDOUT,
    )
    was_stalled = False
    saw_done = False
    last_mtime = time.time()
    try:
        while proc.poll() is None:
            time.sleep(15)
            try:
                mtime = os.path.getmtime(run_log)
            except OSError:
                mtime = last_mtime
            if time.time() - mtime > STALL_SECONDS:
                log("检测到卡死 (日志 %d 秒无更新)，杀掉重启..." % STALL_SECONDS)
                proc.kill()
                proc.wait()
                was_stalled = True
                break
            last_mtime = mtime
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        # 清理本工具拉起的 Chrome，避免残留
        kill_cdp_chrome()
    # 判断是否完成
    try:
        content = run_log.read_text(encoding="utf-8", errors="replace")
        if "no more %s" % feed_type in content or "finished" in content:
            saw_done = True
    except Exception:
        content = ""
    after = count_saved(feed_type)
    log("本次结束: 已存 %s→%s, 卡死=%s, 完成标志=%s" % (before, after, was_stalled, saw_done))
    return before, after, was_stalled, saw_done


def export_feed(feed_type, known_total=0):
    restarts = 0
    prev_count = -1
    no_growth_runs = 0
    while restarts < MAX_RESTARTS:
        run_log = LOG_DIR / ("self_%s_run.log" % feed_type)
        before, after, was_stalled, saw_done = run_once(feed_type, run_log)
        # 已知总量：已存 < 总量 不算完成，继续跑（即使本run打了"完成"标记，可能是假到底）
        if known_total and after < known_total:
            log("%s 已存 %s < 总量 %s，继续" % (feed_type, after, known_total))
            restarts += 1
            prev_count = after
            continue
        if saw_done:
            log("%s 导出完成 (共 %s 条)" % (feed_type, after))
            return
        if known_total and after >= known_total:
            log("%s 达到总量 %s，完成" % (feed_type, known_total))
            return
        # 未知总量（liked）：两轮无新增且非卡死 → 视为到底
        if not was_stalled and after == prev_count and restarts >= 1:
            no_growth_runs += 1
            if no_growth_runs >= 2:
                log("%s 连续两轮无新增且未卡死，视为完成 (共 %s 条)" % (feed_type, after))
                return
        else:
            no_growth_runs = 0
        prev_count = after
        restarts += 1
        log("%s 重启 %s/%s 次" % (feed_type, restarts, MAX_RESTARTS))
    log("%s 达到最大重启次数，暂停" % feed_type)


if __name__ == "__main__":
    LOG_DIR.mkdir(exist_ok=True)
    acquire_lock()
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    if target in ("favorite", "all"):
        export_feed("favorite", known_total=10064)
    if target in ("liked", "all"):
        export_feed("liked", known_total=0)
    try:
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    log("supervisor 全部结束")
