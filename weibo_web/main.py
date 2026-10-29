# -*- coding: utf-8 -*-
"""微博归档前台 - FastAPI 应用。启动: python -m weibo_web.main  (端口 8095)"""
import pathlib
import time

from fastapi import FastAPI, Query, UploadFile, File, Form
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

import httpx
import config
from . import data, tasks, db

app = FastAPI(title="微博归档前台")

STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"
db.init_db()
# 主归档用户（tombkeeper）的全量微博数（statuses_count）
_tk = db.get_user(str(config.WEIBO_CREATOR_ID_LIST[0]))
if _tk and not _tk.get("total_posts"):
    db.set_total_posts(str(config.WEIBO_CREATOR_ID_LIST[0]), 25087)


@app.get("/")
def index():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/meta")
def meta():
    return {
        "collections": {
            c: {"name": data.COLLECTION_NAMES[c], "count": data.count(c),
                "stats": data.stats(c)} for c in data.COLLECTIONS
        },
        "html": {c: tasks.html_status(c) for c in data.COLLECTIONS},
        "tasks": tasks.list_tasks(),
    }


@app.get("/api/posts")
def posts(collection: str = "creator",
          q: str = "", year: str = "", author: str = "", avail: str = "",
          offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=200)):
    if collection not in data.COLLECTIONS and not collection.startswith("user_"):
        return JSONResponse({"error": "bad collection"}, status_code=400)
    return data.search(collection, q, year, author, offset, limit, avail)


@app.get("/api/post")
def post(collection: str = "creator", note_id: str = ""):
    p = data.get_post(collection, note_id)
    if not p:
        return JSONResponse({"error": "not found"}, status_code=404)
    return p


@app.get("/api/stats")
def stats(collection: str = "creator"):
    if collection not in data.COLLECTIONS and not collection.startswith("user_"):
        return JSONResponse({"error": "bad collection"}, status_code=400)
    return data.stats(collection)


@app.get("/api/top")
def top(collection: str = "creator", n: int = 20):
    if collection not in data.COLLECTIONS and not collection.startswith("user_"):
        return JSONResponse({"error": "bad collection"}, status_code=400)
    return data.top_likes(collection, n)


@app.get("/api/tasks")
def list_tasks():
    return tasks.list_tasks()


@app.get("/api/tasks/history")
def task_history():
    return db.recent_tasks(20)


@app.get("/api/social/changes")
def social_changes():
    return db.list_changes(30)


@app.post("/api/tasks/{task_type}/start")
def start_task(task_type: str, mode: str = ""):
    ok, msg = tasks.start_task(task_type, mode)
    return {"ok": ok, "msg": msg}


@app.post("/api/tasks/{task_type}/stop")
def stop_task(task_type: str):
    ok, msg = tasks.stop_task(task_type)
    return {"ok": ok, "msg": msg}


@app.get("/api/tasks/{task_type}/log")
def task_log(task_type: str, n: int = 60):
    return {"lines": tasks.log_tail(task_type, n)}


@app.post("/api/regenerate")
def regenerate(collection: str = "creator"):
    if collection not in data.COLLECTIONS:
        return JSONResponse({"error": "bad collection"}, status_code=400)
    proc = tasks.regenerate_html(collection)
    return {"ok": True, "msg": f"已开始重新生成 {data.COLLECTION_NAMES[collection]} 的 HTML"}


@app.get("/api/regenerate/{collection}/log")
def regen_log(collection: str, n: int = 40):
    return {"lines": tasks.log_tail(f"gen_{collection}", n)}


@app.get("/api/html/{collection}")
def html_file(collection: str):
    out = pathlib.Path("data") / "weibo" / f"{collection}_weibo.html"
    if out.exists():
        return FileResponse(out)
    return JSONResponse({"error": "html not generated"}, status_code=404)


_IMG_CACHE = {}


@app.get("/api/img")
def img_proxy(url: str = ""):
    """图片代理：带微博 Referer 拉取 sinaimg，绕过防盗链。支持本地已下载优先。"""
    # 优先本地已下载（images 目录，按 pic 文件名）
    import os as _os
    name = _os.path.basename(url.split("?")[0])
    local = pathlib.Path("data/weibo/images") / name
    if local.exists():
        return FileResponse(local)
    # 白名单：仅微博图片域
    if not url.startswith(("https://wx", "https://tvax", "https://n.sinaimg", "https://simg.sinaimg")):
        return JSONResponse({"error": "bad url"}, status_code=400)
    if url in _IMG_CACHE:
        return Response(content=_IMG_CACHE[url]["body"], media_type=_IMG_CACHE[url]["ct"])
    try:
        r = httpx.get(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            "Referer": "https://weibo.com/",
        }, timeout=30, verify=False, follow_redirects=True)
        if r.status_code == 200 and r.content:
            ct = r.headers.get("content-type", "image/jpeg")
            if len(r.content) < 5_000_000:  # 缓存小图
                _IMG_CACHE[url] = {"body": r.content, "ct": ct}
            return Response(content=r.content, media_type=ct)
    except Exception:
        pass
    return JSONResponse({"error": "fetch failed"}, status_code=502)


# ===== 关系管理（人脉） =====

@app.get("/api/social")
def social_stats():
    return {"stats": db.stats()}


@app.get("/api/social/users")
def social_users(filt: str = "all", keyword: str = ""):
    users = db.list_users(filt, keyword)
    # 每个用户附带微博条数（便于人脉卡片展示）
    for u in users:
        u["post_count"] = data.count(f"user_{u['user_id']}")
    return users


@app.get("/api/social/users/{user_id}")
def social_user(user_id: str):
    u = db.get_user(user_id)
    if not u:
        return JSONResponse({"error": "user not found"}, status_code=404)
    # 该用户在收藏/赞过里出现的条数（粗略互动统计）
    counts = {}
    for c in data.COLLECTIONS:
        counts[c] = sum(1 for p in data.load_collection(c) if p["nick"] == u.get("screen_name"))
    u["interaction"] = counts
    u["post_count"] = data.count(f"user_{user_id}")
    return u


@app.get("/api/social/users/{user_id}/posts")
def social_user_posts(user_id: str, q: str = "", year: str = "", avail: str = "", offset: int = Query(0, ge=0),
                      limit: int = Query(20, ge=1, le=200)):
    return data.search_user(f"user_{user_id}", q, year, "", offset, limit, avail)


@app.get("/api/social/tracked_collections")
def tracked_collections():
    return data.list_tracked_collections()


@app.post("/api/social/batch_track")
def batch_track():
    """把全部关注（含双向）设为跟踪（增量）。"""
    users = db.list_users("all", "")
    n = 0
    for u in users:
        if u.get("is_following"):
            db.set_tracked(u["user_id"], True, "incremental")
            n += 1
    return {"ok": True, "msg": f"已将 {n} 个关注设为跟踪（增量）"}


@app.post("/api/social/batch_crawl")
def batch_crawl():
    """批量爬取所有已跟踪用户（顺序队列）。"""
    ok, msg = tasks.start_task("batch_crawl")
    return {"ok": ok, "msg": msg if ok else (msg + "（可在任务区启动）")}
    if mode not in ("off", "incremental", "full"):
        return JSONResponse({"error": "bad mode"}, status_code=400)
    # 主归档对象（tombkeeper）已有 creator 全量归档，禁止重复跟踪
    if str(user_id) == str(config.WEIBO_CREATOR_ID_LIST[0]):
        return {"ok": False, "msg": "该用户是主归档对象（creator），请用管理区「tombkeeper 增量更新」更新，不要单用户跟踪"}
    db.set_tracked(user_id, bool(tracked), mode)
    return {"ok": True}


@app.post("/api/social/users/{user_id}/track")
def track_user(user_id: str, tracked: int = 1, mode: str = "incremental"):
    if mode not in ("off", "incremental", "full"):
        return JSONResponse({"error": "bad mode"}, status_code=400)
    db.set_tracked(user_id, bool(tracked), mode)
    return {"ok": True}


@app.post("/api/social/users/{user_id}/crawl")
def crawl_tracked_user(user_id: str, mode: str = "incremental", media: int = 0):
    if str(user_id) == str(config.WEIBO_CREATOR_ID_LIST[0]):
        return {"ok": False, "msg": "该用户是主归档对象（creator），请用「tombkeeper 更新」"}
    ok, msg = tasks.start_track(user_id, mode, bool(media))
    return {"ok": ok, "msg": msg + ("（含媒体下载）" if media and ok else "")}


@app.post("/api/social/users/add")
async def add_user(body: dict):
    raw = str(body.get("id_or_url", "")).strip()
    mode = body.get("mode", "incremental")
    crawl_now = bool(body.get("crawl", False))
    media = bool(body.get("media", False))
    if mode not in ("incremental", "full"):
        return JSONResponse({"error": "bad mode"}, status_code=400)
    # 解析 id：纯数字 / weibo.com/u/{id} / weibo.com/{id} / m.weibo.cn/u/{id}
    import re
    m = re.search(r"(\d{6,})", raw)
    if not m:
        return {"ok": False, "msg": "无法解析用户 id，请粘贴纯数字 id 或微博主页链接"}
    user_id = m.group(1)
    if str(user_id) == str(config.WEIBO_CREATOR_ID_LIST[0]):
        return {"ok": False, "msg": "该用户是主归档对象（creator），请用「tombkeeper 更新」"}
    db.set_tracked(user_id, True, mode)
    # 后台拉取用户信息（昵称/头像），填占位避免阻塞
    tasks.start_fetch_user(user_id)
    if crawl_now:
        tasks.start_track(user_id, mode, media)
    return {"ok": True, "user_id": user_id, "msg": "已加入跟踪" + ("并启动爬取" if crawl_now else "")}


@app.get("/api/social/users/{user_id}/crawl")
def crawl_status(user_id: str):
    return tasks.track_status(user_id)


@app.get("/api/social/users/{user_id}/crawl/log")
def crawl_log(user_id: str, n: int = 60):
    return {"lines": tasks.track_log(user_id, n)}


@app.get("/api/social/tasks")
def social_tasks():
    return db.recent_tasks(20)


# ===== 数据迁移（多机协作：导出/导入合并） =====

_UPLOAD_DIR = pathlib.Path("data/weibo/exports")


def _ensure_upload_dir():
    _UPLOAD_DIR.mkdir(parents=True, exist_ok=True)


def _safe_export_filename(name: str) -> str:
    name = pathlib.Path(name).name
    if not name.endswith(".zip") or ".." in name:
        raise ValueError("bad filename")
    return name


@app.get("/api/transfer/options")
def transfer_options():
    """导出可选用户/集合 + 策略 + 已删统计。"""
    users = []
    # 主归档 + 跟踪用户
    seen = set()
    for u in db.list_users("all", ""):
        if str(u.get("user_id")) in seen:
            continue
        seen.add(str(u.get("user_id")))
        users.append({"user_id": str(u.get("user_id")), "screen_name": u.get("screen_name") or u.get("user_id")})
    # 归档里实际存在的作者（未在关系库的补充）
    from store.weibo import jsonl_ops
    recs, _ = jsonl_ops.load_all_content_records()
    authors = {}
    for r in recs.values():
        uid = str(r.get("user_id") or "")
        if uid and uid not in seen:
            authors[uid] = r.get("nickname") or uid
    for uid, nick in sorted(authors.items()):
        users.append({"user_id": uid, "screen_name": nick})
    collections = [{"key": "creator", "name": "tombkeeper 归档"},
                   {"key": "favorite", "name": "我的收藏"},
                   {"key": "liked", "name": "我的赞过"}]
    seen_keys = {"creator", "favorite", "liked"}
    for t in data.list_tracked_collections():
        if t["key"] not in seen_keys:
            seen_keys.add(t["key"])
            collections.append({"key": t["key"], "name": t["screen_name"]})
    return {
        "users": users,
        "collections": collections,
        "policies": ["integrity", "freshness", "source_priority", "keep_both"],
        "deleted_count": data.count_availability_deleted(),
        "export_dir": str(_UPLOAD_DIR),
        "save_policy": config.WEIBO_SAVE_POLICY,
        "scan_deleted": config.WEIBO_SCAN_DELETED,
    }


@app.post("/api/transfer/export")
def do_export(body: dict):
    from store.weibo import transfer
    users = [str(x) for x in (body.get("users") or []) if str(x).strip()]
    collections = [str(x) for x in (body.get("collections") or []) if str(x).strip()]
    try:
        zip_path = transfer.export(users=users or None, collections=collections or None,
                                   dest=str(_UPLOAD_DIR))
        data.reload_all()
        return {"ok": True, "path": zip_path, "filename": pathlib.Path(zip_path).name}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.get("/api/transfer/download/{filename}")
def download_export(filename: str):
    name = _safe_export_filename(filename)
    full = _UPLOAD_DIR / name
    if not full.exists():
        return JSONResponse({"error": "not found"}, status_code=404)
    return FileResponse(full, filename=name, media_type="application/zip")


@app.post("/api/transfer/import/preview")
async def import_preview(file: UploadFile = File(...), policy: str = Form("integrity")):
    """上传导出包并预览合并报告（dry-run，不落盘）。"""
    from store.weibo import transfer
    _ensure_upload_dir()
    safe = _safe_export_filename(file.filename)
    dest = _UPLOAD_DIR / f"upload_{int(time.time())}_{safe}"
    content = await file.read()
    dest.write_bytes(content)
    try:
        report = transfer.import_(str(dest), policy=policy, dry_run=True)
        return {"ok": True, "upload_file": dest.name, "report": report}
    except Exception as e:
        dest.unlink(missing_ok=True)
        return {"ok": False, "msg": str(e)}


@app.post("/api/transfer/import/confirm")
def import_confirm(body: dict):
    """确认执行合并（依据预览时保留的包）。"""
    from store.weibo import transfer
    upload_file = str(body.get("upload_file") or "")
    policy = str(body.get("policy") or "integrity")
    full = (_UPLOAD_DIR / _safe_export_filename(upload_file)) if upload_file else None
    if not full or not full.exists():
        return {"ok": False, "msg": "找不到上传包，请重新上传"}
    try:
        report = transfer.import_(str(full), policy=policy, dry_run=False)
        data.reload_all()
        return {"ok": True, "report": report}
    except Exception as e:
        return {"ok": False, "msg": str(e)}


@app.post("/api/transfer/flag")
def transfer_flag(body: dict):
    """手动标记 availability（半自动已删管理）。"""
    from store import weibo
    note_ids = [str(x) for x in (body.get("note_ids") or []) if str(x).strip()]
    status = str(body.get("status") or "deleted_on_source")
    if status not in ("ok", "deleted_on_source"):
        return {"ok": False, "msg": "bad status"}
    if not note_ids:
        return {"ok": False, "msg": "no note_ids"}
    n = weibo.mark_availability(note_ids, status)
    data.reload_all()
    return {"ok": True, "marked": n, "deleted_count": data.count_availability_deleted()}


@app.get("/api/transfer/uploads")
def list_uploads():
    """列出上传/导出的包（便于清理）。"""
    _ensure_upload_dir()
    items = []
    for p in sorted(_UPLOAD_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)[:20]:
        items.append({"name": p.name, "size_mb": round(p.stat().st_size / 1048576, 2),
                      "mtime": p.stat().st_mtime})
    return {"items": items}


@app.post("/api/transfer/cleanup")
def cleanup_uploads(body: dict = None):
    """清理上传包（可选保留最近 N 个）。"""
    body = body or {}
    keep = int(body.get("keep", 5))
    _ensure_upload_dir()
    removed = 0
    zips = sorted(_UPLOAD_DIR.glob("*.zip"), key=lambda x: x.stat().st_mtime, reverse=True)
    for p in zips[keep:]:
        try:
            p.unlink()
            removed += 1
        except Exception:
            pass
    return {"ok": True, "removed": removed}


@app.get("/api/login/qrcode")
def login_qrcode():
    """云上扫码用：爬虫登录时生成的二维码图片（data/login_qrcode.png）。"""
    p = pathlib.Path("data") / "login_qrcode.png"
    if p.exists():
        return FileResponse(p, media_type="image/png")
    return JSONResponse({"error": "no qrcode yet"}, status_code=404)


app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
