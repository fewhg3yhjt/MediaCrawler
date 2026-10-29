# -*- coding: utf-8 -*-
"""微博归档前台 - 任务管理：启动/停止抓取、重新生成 HTML、查看进度。单实例锁。"""
import os
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
if sys.platform == "win32":
    PY = ROOT / ".venv" / "Scripts" / "python.exe"
else:
    PY = ROOT / ".venv" / "bin" / "python"
LOG_DIR = ROOT / "logs"
IMAGES_DIR = ROOT / "data" / "weibo" / "images"

TASK_TYPES = ["favorite", "liked", "creator", "social", "batch_crawl"]
TASK_NAMES = {"favorite": "抓取收藏", "liked": "抓取赞过", "creator": "tombkeeper 更新", "social": "同步关注/粉丝", "batch_crawl": "批量爬取跟踪用户"}

_running = {}  # type -> {"proc": Popen, "start": float}


def _chrome_cleanup():
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process -Filter \"Name='chrome.exe'\" | "
                 "Where-Object { $_.CommandLine -match 'cdp_wb_user_data_dir' } | "
                 "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"],
                capture_output=True, timeout=30,
            )
        else:
            for pattern in ("cdp_wb_user_data_dir", "remote-debugging-port"):
                subprocess.run(["pkill", "-f", pattern],
                               capture_output=True, timeout=30)
    except Exception:
        pass


def is_running(task_type: str) -> bool:
    p = _running.get(task_type)
    if p and p["proc"].poll() is None:
        return True
    if p:
        _running.pop(task_type, None)
    return False


def list_tasks():
    from . import data as _data
    # 已知总量（用于进度条）：favorite=接口总数；creator=主归档目标条数
    try:
        from . import db as _db
        import config as _cfg
        tk = _db.get_user(str(_cfg.WEIBO_CREATOR_ID_LIST[0]))
        creator_total = int((tk or {}).get("total_posts") or 0) or 0
    except Exception:
        creator_total = 0
    known_total = {"favorite": 10064, "creator": creator_total or _data.count("creator")}
    result = []
    for t in TASK_TYPES:
        running = is_running(t)
        log = LOG_DIR / f"web_{t}.log"
        last_line = ""
        if log.exists():
            try:
                lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
                last_line = lines[-1][:120] if lines else ""
            except Exception:
                pass
        # 实时条数：任务运行中用 live_count（不依赖服务缓存），非运行用缓存计数
        count = _data.live_count(t) if t in ("favorite", "liked", "creator") else None
        result.append({
            "type": t,
            "name": TASK_NAMES[t],
            "running": running,
            "start": _running.get(t, {}).get("start"),
            "last_log": last_line,
            "count": count,
            "total": known_total.get(t, 0),
        })
    # 运行中的单用户爬取任务
    for key, info in list(_running.items()):
        if key.startswith("track_") and info["proc"].poll() is None:
            user_id = key.replace("track_", "")
            from . import db as _db
            u = _db.get_user(user_id)
            result.append({
                "type": key,
                "name": "📌 爬取 " + (u["screen_name"] if u else user_id),
                "running": True,
                "start": info["start"],
                "last_log": "",
                "count": _data.live_count(f"user_{user_id}"),
                "total": int((u or {}).get("total_posts") or 0) or 0,
            })
    return result


def start_task(task_type: str, mode: str = ""):
    if task_type not in TASK_TYPES:
        return False, "unknown task type"
    if is_running(task_type):
        return False, "该任务已在运行（单实例锁）"
    log = LOG_DIR / f"web_{task_type}.log"
    LOG_DIR.mkdir(exist_ok=True)
    env = dict(os.environ)
    if task_type == "creator" and mode == "full":
        env["WEIBO_FULL_CRAWL"] = "1"
    elif task_type == "creator":
        env.pop("WEIBO_FULL_CRAWL", None)
    if task_type == "social":
        cmd = [str(PY), "scripts/sync_follows_fans.py"]
    elif task_type == "batch_crawl":
        cmd = [str(PY), "scripts/batch_crawl_tracked.py"]
    else:
        cmd = [str(PY), "main.py", "--platform", "wb", "--lt", "qrcode",
               "--type", task_type, "--save_data_option", "jsonl"]
    proc = subprocess.Popen(
        cmd,
        cwd=str(ROOT),
        env=env,
        stdout=open(log, "wb"),
        stderr=subprocess.STDOUT,
    )
    _running[task_type] = {"proc": proc, "start": time.time()}
    return True, "已启动"


def start_track(user_id: str, mode: str = "incremental", media: bool = False):
    """爬取指定用户微博（单用户单实例）。media=True 时下载图片/视频。"""
    key = f"track_{user_id}"
    if is_running(key):
        return False, "该用户爬取已在运行"
    log = LOG_DIR / f"web_track_{user_id}.log"
    LOG_DIR.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [str(PY), "scripts/crawl_user.py", str(user_id), mode, "1" if media else "0"],
        cwd=str(ROOT),
        stdout=open(log, "wb"),
        stderr=subprocess.STDOUT,
    )
    _running[key] = {"proc": proc, "start": time.time()}
    return True, "已启动"


def track_status(user_id: str):
    return {"running": is_running(f"track_{user_id}")}


def track_log(user_id: str, n: int = 60):
    return log_tail(f"track_{user_id}", n)


def start_fetch_user(user_id: str):
    """后台拉取用户基本信息（写关系库）。"""
    key = f"fetch_{user_id}"
    if is_running(key):
        return False, "正在拉取"
    log = LOG_DIR / f"web_fetch_{user_id}.log"
    LOG_DIR.mkdir(exist_ok=True)
    proc = subprocess.Popen(
        [str(PY), "scripts/fetch_user_info.py", str(user_id)],
        cwd=str(ROOT),
        stdout=open(log, "wb"),
        stderr=subprocess.STDOUT,
    )
    _running[key] = {"proc": proc, "start": time.time()}
    return True, "已启动"


def stop_task(task_type: str):
    if not is_running(task_type):
        return False, "任务未在运行"
    p = _running.get(task_type, {}).get("proc")
    try:
        p.kill()
        p.wait(timeout=10)
    except Exception:
        pass
    _running.pop(task_type, None)
    _chrome_cleanup()
    return True, "已停止"


def log_tail(task_type: str, n: int = 60):
    log = LOG_DIR / f"web_{task_type}.log"
    if not log.exists():
        return []
    try:
        lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        return lines[-n:]
    except Exception:
        return []


def regenerate_html(collection: str):
    """重新生成指定集合的 HTML 归档。返回是否已启动。"""
    from . import data
    log = LOG_DIR / f"web_gen_{collection}.log"
    LOG_DIR.mkdir(exist_ok=True)
    out = ROOT / "data" / "weibo" / f"{collection}_weibo.html"
    title = data.COLLECTION_NAMES.get(collection, collection)
    proc = subprocess.Popen(
        [str(PY), "tools/weibo_jsonl_to_html.py",
         "--input", str(ROOT / f"data/weibo/jsonl/{collection}_contents_*.jsonl"),
         "--output", str(out), "--title", title, "--download-images", "--workers", "6"],
        cwd=str(ROOT),
        stdout=open(log, "wb"),
        stderr=subprocess.STDOUT,
    )
    return proc


def html_status(collection: str):
    out = ROOT / "data" / "weibo" / f"{collection}_weibo.html"
    if not out.exists():
        return {"exists": False}
    import os
    return {"exists": True, "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(out.stat().st_mtime)),
            "size_mb": round(out.stat().st_size / 1024 / 1024, 1)}
