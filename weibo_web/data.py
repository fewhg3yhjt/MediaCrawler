# -*- coding: utf-8 -*-
"""微博归档前台 - 数据层：读 jsonl，提供列表/搜索/统计。"""
import glob
import json
import pathlib
import re

DATA_DIR = pathlib.Path(__file__).resolve().parent.parent / "data" / "weibo" / "jsonl"
COLLECTIONS = ["creator", "favorite", "liked"]
COLLECTION_NAMES = {"creator": "tombkeeper 归档", "favorite": "我的收藏", "liked": "我的赞过"}

_cache = {}


def _parse_imgs(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            d = json.loads(value)
            if isinstance(d, list):
                return d
        except Exception:
            pass
    return []


def load_collection(collection: str, force: bool = False):
    if collection in _cache and not force:
        return _cache[collection]
    posts = []
    seen = set()
    for path in sorted(glob.glob(str(DATA_DIR / f"{collection}_contents_*.jsonl"))):
        try:
            for line in open(path, encoding="utf-8"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                nid = str(d.get("note_id", ""))
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                posts.append({
                    "id": str(d["note_id"]),
                    "c": d.get("content", "") or "",
                    "dt": d.get("create_date_time", ""),
                    "nick": d.get("nickname", ""),
                    "l": str(d.get("liked_count", "0")),
                    "cc": str(d.get("comments_count", "0")),
                    "sh": str(d.get("shared_count", "0")),
                    "url": d.get("note_url", ""),
                    "imgs": _parse_imgs(d.get("image_urls")),
                    "ip": d.get("ip_location", ""),
                    "src": d.get("source_type", ""),
                    "rt": d.get("is_retweet", "0"),
                    "rrid": str(d.get("retweeted_note_id", "") or ""),
                    "rtn": d.get("rt_nick", "") or "",
                    "rtc": d.get("rt_content", "") or "",
                    "rtt": d.get("rt_time", "") or "",
                    "rtimgs": _parse_imgs(d.get("rt_images")),
                    "av": d.get("availability", "ok"),
                    "vid": d.get("video_url", "") or "",
                })
        except Exception:
            continue
    posts.sort(key=lambda p: p["dt"], reverse=True)
    _cache[collection] = posts
    return posts


def reload_all():
    _cache.clear()
    for c in COLLECTIONS:
        load_collection(c, force=True)


def count(collection: str) -> int:
    return len(load_collection(collection))


_NID_RE = re.compile(r'"note_id"\s*:\s*"?(\d+)"?')


def live_count(collection: str) -> int:
    """实时去重条数：直接扫 jsonl 文件统计 note_id（不依赖内存缓存）。

    供任务进度轮询使用——爬取子进程写文件后这里立刻能反映最新条数。
    """
    seen = set()
    for path in sorted(glob.glob(str(DATA_DIR / f"{collection}_contents_*.jsonl"))):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    m = _NID_RE.search(line)
                    if m:
                        seen.add(m.group(1))
                        continue
                    try:
                        nid = json.loads(line).get("note_id")
                        if nid:
                            seen.add(str(nid))
                    except Exception:
                        continue
        except Exception:
            continue
    return len(seen)


def stats(collection: str):
    posts = load_collection(collection)
    years = {}
    for p in posts:
        y = p["dt"][:4] or "?"
        years[y] = years.get(y, 0) + 1
    return {
        "total": len(posts),
        "years": dict(sorted(years.items(), reverse=True)),
        "first": posts[-1]["dt"] if posts else "",
        "last": posts[0]["dt"] if posts else "",
    }


def search(collection: str, q: str = "", year: str = "", author: str = "",
           offset: int = 0, limit: int = 20, avail: str = ""):
    posts = load_collection(collection)
    q = (q or "").strip().lower()
    author = (author or "").strip()
    avail = (avail or "").strip()
    matched = []
    for p in posts:
        if q and q not in p["c"].lower():
            continue
        if year and p["dt"][:4] != year:
            continue
        if author and author not in p["nick"]:
            continue
        if avail == "deleted" and p["av"] != "deleted_on_source":
            continue
        if avail == "ok" and p["av"] != "ok":
            continue
        matched.append(p)
    total = len(matched)
    page = matched[offset:offset + limit]
    return {"total": total, "posts": page}


def get_post(collection: str, note_id: str):
    for p in load_collection(collection):
        if p["id"] == note_id:
            return p
    return None


def top_likes(collection: str, n: int = 20):
    posts = load_collection(collection)
    def _lk(p):
        try:
            return int(p["l"])
        except Exception:
            return 0
    return sorted(posts, key=_lk, reverse=True)[:n]


# ===== 单用户（跟踪）微博 =====

def list_tracked_collections():
    """已跟踪用户及其微博条数，供前端作为数据源切换。"""
    from . import db
    result = []
    for u in db.list_users("tracked", ""):
        result.append({
            "key": f"user_{u['user_id']}",
            "user_id": u["user_id"],
            "screen_name": u["screen_name"],
            "count": count(f"user_{u['user_id']}"),
        })
    return result


def search_user(user_key: str, q: str = "", year: str = "", author: str = "",
                offset: int = 0, limit: int = 20, avail: str = ""):
    return search(user_key, q, year, author, offset, limit, avail)


def count_availability_deleted():
    """跨所有集合统计 availability=deleted_on_source 的去重记录数。"""
    from store import weibo
    try:
        return weibo.count_availability("deleted_on_source")
    except Exception:
        return 0
