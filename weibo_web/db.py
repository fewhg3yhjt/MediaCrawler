# -*- coding: utf-8 -*-
"""关系管理系统 - SQLite 数据层：用户、关系、任务。"""
import pathlib
import sqlite3
import time

DB_PATH = pathlib.Path(__file__).resolve().parent / "weibo_relations.db"
_UPSERT_COUNT = 0


def _conn():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    with _conn() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            screen_name  TEXT,
            avatar       TEXT,
            description  TEXT,
            location     TEXT,
            is_following INTEGER DEFAULT 0,
            is_follower  INTEGER DEFAULT 0,
            tracked      INTEGER DEFAULT 0,
            crawl_mode   TEXT DEFAULT 'off',      -- off | incremental | full
            last_crawled TEXT,
            created_at   TEXT
        );
        CREATE TABLE IF NOT EXISTS tasks (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            type          TEXT,
            name          TEXT DEFAULT '',
            target_user   TEXT DEFAULT '',
            status        TEXT DEFAULT 'pending', -- running | done | failed | stopped
            started_at    TEXT,
            finished_at   TEXT,
            detail        TEXT
        );
        CREATE TABLE IF NOT EXISTS relations_history (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            change_type TEXT,      -- new_following | unfollowed | new_follower | lost_follower
            user_id     TEXT,
            screen_name TEXT,
            detected_at TEXT
        );
        """)
        # 迁移：旧 tasks 表补 name 列
        cols = [r[1] for r in c.execute("PRAGMA table_info(tasks)")]
        if "name" not in cols:
            c.execute("ALTER TABLE tasks ADD COLUMN name TEXT DEFAULT ''")
        # 迁移：users 表补 total_posts（全量微博数）
        ucols = [r[1] for r in c.execute("PRAGMA table_info(users)")]
        if "total_posts" not in ucols:
            c.execute("ALTER TABLE users ADD COLUMN total_posts INTEGER DEFAULT 0")


def record_change(change_type: str, user_id: str, screen_name: str):
    with _conn() as c:
        c.execute("INSERT INTO relations_history (change_type, user_id, screen_name, detected_at) VALUES (?,?,?,?)",
                  (change_type, user_id, screen_name, time.strftime("%Y-%m-%d %H:%M:%S")))


def list_changes(limit: int = 30):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM relations_history ORDER BY id DESC LIMIT ?", (limit,))]


def upsert_user(u):
    global _UPSERT_COUNT
    _UPSERT_COUNT += 1
    if _UPSERT_COUNT % 50 == 1:
        print(f"[db.upsert_user] called {_UPSERT_COUNT}: {u.get('user_id')} {u.get('screen_name','')[:15]}", flush=True)
    with _conn() as c:
        c.execute("""
            INSERT INTO users (user_id, screen_name, avatar, description, location, created_at)
            VALUES (?,?,?,?,?,?)
            ON CONFLICT(user_id) DO UPDATE SET
                screen_name=excluded.screen_name, avatar=excluded.avatar,
                description=excluded.description, location=excluded.location
        """, (u.get("user_id"), u.get("screen_name"), u.get("avatar"),
              u.get("description"), u.get("location"), time.strftime("%Y-%m-%d %H:%M:%S")))
        n = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        if _UPSERT_COUNT % 50 == 1:
            print(f"[db.upsert_user] same-conn count={n}", flush=True)


def set_relation(user_id: str, is_following=None, is_follower=None):
    with _conn() as c:
        if is_following is not None:
            c.execute("UPDATE users SET is_following=? WHERE user_id=?", (1 if is_following else 0, user_id))
        if is_follower is not None:
            c.execute("UPDATE users SET is_follower=? WHERE user_id=?", (1 if is_follower else 0, user_id))


def set_tracked(user_id: str, tracked: bool, crawl_mode: str):
    with _conn() as c:
        c.execute("UPDATE users SET tracked=?, crawl_mode=? WHERE user_id=?",
                  (1 if tracked else 0, crawl_mode, user_id))


def set_total_posts(user_id: str, total: int):
    with _conn() as c:
        c.execute("UPDATE users SET total_posts=? WHERE user_id=?", (int(total or 0), user_id))


def set_user_meta(user_id: str, **kw):
    """更新用户元信息（total_posts 等）。"""
    if not kw:
        return
    cols = ", ".join(f"{k}=?" for k in kw)
    with _conn() as c:
        c.execute(f"UPDATE users SET {cols} WHERE user_id=?", (*kw.values(), user_id))


def relation_type(r):
    if r["is_following"] and r["is_follower"]:
        return "mutual"
    if r["is_following"]:
        return "following"
    if r["is_follower"]:
        return "follower"
    return "none"


def list_users(filt: str = "all", keyword: str = ""):
    sql = "SELECT * FROM users WHERE 1=1"
    params = []
    if filt == "following":
        sql += " AND is_following=1"
    elif filt == "follower":
        sql += " AND is_follower=1"
    elif filt == "mutual":
        sql += " AND is_following=1 AND is_follower=1"
    elif filt == "tracked":
        sql += " AND tracked=1"
    if keyword:
        sql += " AND screen_name LIKE ?"
        params.append("%" + keyword + "%")
    sql += " ORDER BY tracked DESC, screen_name"
    with _conn() as c:
        rows = [dict(r) for r in c.execute(sql, params)]
    for r in rows:
        r["relation"] = relation_type(r)
    return rows


def get_user(user_id: str):
    with _conn() as c:
        r = c.execute("SELECT * FROM users WHERE user_id=?", (user_id,)).fetchone()
    if not r:
        return None
    d = dict(r)
    d["relation"] = relation_type(d)
    return d


def stats():
    with _conn() as c:
        total = c.execute("SELECT COUNT(*) n FROM users").fetchone()["n"]
        following = c.execute("SELECT COUNT(*) n FROM users WHERE is_following=1").fetchone()["n"]
        follower = c.execute("SELECT COUNT(*) n FROM users WHERE is_follower=1").fetchone()["n"]
        mutual = c.execute("SELECT COUNT(*) n FROM users WHERE is_following=1 AND is_follower=1").fetchone()["n"]
        tracked = c.execute("SELECT COUNT(*) n FROM users WHERE tracked=1").fetchone()["n"]
        last_sync = c.execute("SELECT MAX(started_at) s FROM tasks WHERE type='social'").fetchone()["s"]
    return {"total": total, "following": following, "follower": follower,
            "mutual": mutual, "tracked": tracked, "last_sync": last_sync}


def add_task(task_type: str, target_user: str = "", name: str = ""):
    with _conn() as c:
        cur = c.execute(
            "INSERT INTO tasks (type, name, target_user, status, started_at) VALUES (?,?,?,?,?)",
            (task_type, name or task_type, target_user, "running", time.strftime("%Y-%m-%d %H:%M:%S")))
        return cur.lastrowid


def finish_task(task_id, status="done", detail=""):
    with _conn() as c:
        c.execute("UPDATE tasks SET status=?, finished_at=?, detail=? WHERE id=?",
                  (status, time.strftime("%Y-%m-%d %H:%M:%S"), detail[:200], task_id))


def recent_tasks(limit: int = 20):
    with _conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]
