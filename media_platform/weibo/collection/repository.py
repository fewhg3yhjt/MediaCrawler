# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\repository.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#
# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

"""SQLite task records and an atomic, content-addressed image repository."""

import hashlib
import json
import os
import re
import sqlite3
import tempfile
import uuid
from dataclasses import asdict
from contextlib import contextmanager
from pathlib import Path

from .models import (
    CollectionError, CollectionResult, DownloadedAsset, MediaAsset, ImageManifest, PostRef, PostText,
)


class SqliteTaskRepository:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        with self._connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY, request TEXT NOT NULL, scope TEXT NOT NULL,
                    collection TEXT NOT NULL, status TEXT NOT NULL, stop_reason TEXT,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS task_posts (
                    task_id TEXT NOT NULL, note_id TEXT NOT NULL, snapshot TEXT NOT NULL,
                    PRIMARY KEY (task_id, note_id)
                );
                CREATE TABLE IF NOT EXISTS collection_items (
                    scope TEXT NOT NULL, collection TEXT NOT NULL, note_id TEXT NOT NULL,
                    PRIMARY KEY (scope, collection, note_id)
                );
                CREATE TABLE IF NOT EXISTS capability_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
                    note_id TEXT NOT NULL, capability TEXT NOT NULL, options TEXT NOT NULL,
                    status TEXT NOT NULL, result TEXT NOT NULL,
                    created_at TEXT DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS capability_lookup
                ON capability_runs(task_id, note_id, capability, id);
            """)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            with db:
                yield db
        finally:
            db.close()

    def create(self, request_json: str, scope: str, collection: str) -> str:
        task_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO tasks(id,request,scope,collection,status) VALUES(?,?,?,?,?)",
                       (task_id, request_json, scope, collection, "running"))
        return task_id

    def resume(self, task_id: str, request_json: str, scope: str) -> None:
        with self._connect() as db:
            row = db.execute("SELECT request,scope FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row != (request_json, scope):
                raise ValueError("resume requires an existing task with identical request and account scope")
            db.execute("UPDATE tasks SET status='running',stop_reason=NULL WHERE id=?", (task_id,))

    def discover(self, task_id: str, post: PostRef) -> None:
        with self._connect() as db:
            db.execute("INSERT OR IGNORE INTO task_posts VALUES(?,?,?)",
                       (task_id, post.note_id, json.dumps(asdict(post), ensure_ascii=False)))
            db.execute("""INSERT OR IGNORE INTO collection_items
                SELECT scope,collection,? FROM tasks WHERE id=?""", (post.note_id, task_id))

    def pending(self, task_id: str) -> list[PostRef]:
        with self._connect() as db:
            rows = db.execute("SELECT snapshot FROM task_posts WHERE task_id=? ORDER BY rowid", (task_id,)).fetchall()
        posts = []
        for (snapshot,) in rows:
            item = json.loads(snapshot)
            manifest = item.get("images")
            if manifest is not None:
                manifest = ImageManifest(
                    tuple(MediaAsset(**a) for a in manifest["assets"]),
                    manifest["originals_complete"], manifest["retweeted_complete"],
                )
            text = item.get("text")
            if text is not None:
                text = PostText(**text)
            posts.append(PostRef(item["note_id"], manifest, text))
        return posts

    def completed(self, task_id: str, post: PostRef, capability: str) -> bool:
        with self._connect() as db:
            row = db.execute("""SELECT status FROM capability_runs
                WHERE task_id=? AND note_id=? AND capability=? ORDER BY id DESC LIMIT 1""",
                (task_id, post.note_id, capability)).fetchone()
        return row is not None and row[0] in ("succeeded", "no_content")

    def record(self, task_id: str, post: PostRef, capability: str, options_json: str, result: CollectionResult) -> None:
        with self._connect() as db:
            db.execute("""INSERT INTO capability_runs
                (task_id,note_id,capability,options,status,result) VALUES(?,?,?,?,?,?)""",
                (task_id, post.note_id, capability, options_json, result.status,
                 json.dumps(asdict(result), ensure_ascii=False)))

    def finish(self, task_id: str, status: str, stop_reason: str) -> None:
        with self._connect() as db:
            db.execute("UPDATE tasks SET status=?,stop_reason=? WHERE id=?", (status, stop_reason, task_id))

    def report(self, task_id: str) -> dict:
        with self._connect() as db:
            row = db.execute("SELECT status,stop_reason FROM tasks WHERE id=?", (task_id,)).fetchone()
            if row is None:
                raise ValueError("unknown task")
            count = db.execute("SELECT COUNT(*) FROM task_posts WHERE task_id=?", (task_id,)).fetchone()[0]
            latest = db.execute("""SELECT capability,status,COUNT(*) FROM capability_runs
                WHERE id IN (SELECT MAX(id) FROM capability_runs WHERE task_id=? GROUP BY note_id,capability)
                GROUP BY capability,status""", (task_id,)).fetchall()
            results = db.execute("""SELECT note_id,capability,result FROM capability_runs
                WHERE id IN (SELECT MAX(id) FROM capability_runs WHERE task_id=? GROUP BY note_id,capability)
                ORDER BY note_id,capability""", (task_id,)).fetchall()
        return {"task_id": task_id, "status": row[0], "stop_reason": row[1],
                "discovered": count, "capabilities": [
                    {"name": name, "status": status, "count": n} for name, status, n in latest
                ], "results": [
                    {"note_id": note_id, "capability": capability, **json.loads(result)}
                    for note_id, capability, result in results
                ]}


class FileAssetRepository:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.index = self.root / "assets.sqlite3"

    @contextmanager
    def _connect(self):
        self.root.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(self.index, timeout=30)
        try:
            with db:
                db.execute("""CREATE TABLE IF NOT EXISTS assets (
                    scope TEXT, owner TEXT, asset TEXT, kind TEXT, filename TEXT, checksum TEXT,
                    PRIMARY KEY(scope,owner,asset,kind))""")
                yield db
        finally:
            db.close()

    def find(self, scope: str, asset: MediaAsset) -> Path | None:
        try:
            with self._connect() as db:
                row = db.execute("SELECT filename,checksum FROM assets WHERE scope=? AND owner=? AND asset=? AND kind=?",
                                 (scope, asset.owner_post_id, asset.asset_id, asset.kind)).fetchone()
            if row is None:
                return None
            path = self.root / row[0]
            if path.parent != self.root or not path.is_file():
                return None
            return path if hashlib.sha256(path.read_bytes()).hexdigest() == row[1] else None
        except OSError as exc:
            raise CollectionError("asset_read_failed") from exc

    def save(self, scope: str, asset: MediaAsset, data: DownloadedAsset) -> Path:
        if re.fullmatch(r"[a-z0-9]{1,10}", data.extension) is None or not data.content:
            raise CollectionError("invalid_asset_data")
        checksum = hashlib.sha256(data.content).hexdigest()
        target = self.root / f"{checksum}.{data.extension}"
        temporary = None
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.root, delete=False) as stream:
                temporary = stream.name
                stream.write(data.content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, target)
            with self._connect() as db:
                db.execute("INSERT OR REPLACE INTO assets VALUES(?,?,?,?,?,?)",
                           (scope, asset.owner_post_id, asset.asset_id, asset.kind, target.name, checksum))
        except OSError as exc:
            raise CollectionError("asset_write_failed") from exc
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
        return target
