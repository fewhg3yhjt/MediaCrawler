# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\web_jobs.py
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

"""Durable web job lifecycle, independent of HTTP and browser automation."""

import asyncio
import json
import sqlite3
import uuid
from dataclasses import asdict, dataclass
from contextlib import contextmanager
from pathlib import Path
from typing import Protocol

from .models import CollectionError, ProgressEvent


@dataclass
class ImageWebJob:
    id: str
    request: dict
    source_url: str
    use_browser: bool = False
    phase: str = "queued"
    task_id: str | None = None
    scope: str | None = None
    completed: int = 0
    total: int | None = None
    error_code: str | None = None
    report: dict | None = None
    attempts: int = 0


class WebJobRepository:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute("CREATE TABLE IF NOT EXISTS image_web_jobs (id TEXT PRIMARY KEY, document TEXT NOT NULL)")

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path)
        try:
            with db:
                yield db
        finally:
            db.close()

    def save(self, job: ImageWebJob):
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO image_web_jobs VALUES (?,?)",
                       (job.id, json.dumps(asdict(job), ensure_ascii=False)))

    def get(self, job_id: str) -> ImageWebJob:
        with self._connect() as db:
            row = db.execute("SELECT document FROM image_web_jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return ImageWebJob(**json.loads(row[0]))

    def list(self) -> list[ImageWebJob]:
        with self._connect() as db:
            rows = db.execute("SELECT document FROM image_web_jobs ORDER BY rowid DESC").fetchall()
        return [ImageWebJob(**json.loads(row[0])) for row in rows]

    def recover(self):
        for job in self.list():
            if job.phase in ("queued", "collecting", "downloading", "waiting_login"):
                job.phase = "interrupted"
                job.error_code = "service_restarted"
                self.save(job)


class ImageJobExecutor(Protocol):
    async def execute(self, job: ImageWebJob, progress, on_qr, login: bool) -> dict: ...


class ImageJobController:
    def __init__(self, repository: WebJobRepository, executor: ImageJobExecutor):
        self.repository = repository
        self.executor = executor
        self.repository.recover()
        self.active: asyncio.Task | None = None
        self.active_id: str | None = None
        self.qr: dict[str, tuple[int, bytes]] = {}

    def _available(self):
        if self.active is not None and not self.active.done():
            raise CollectionError("another_job_running")

    def submit(self, request: dict, source_url: str, use_browser: bool) -> ImageWebJob:
        self._available()
        job = ImageWebJob(uuid.uuid4().hex, request, source_url, use_browser)
        self.repository.save(job)
        self._start(job, False)
        return job

    def retry(self, job_id: str, login: bool, consent: bool):
        self._available()
        job = self.repository.get(job_id)
        if job.phase == "completed":
            raise CollectionError("job_already_completed")
        if login and not consent:
            raise CollectionError("login_consent_required")
        if login:
            job.use_browser = True
        job.phase, job.error_code = "queued", None
        self._start(job, login)

    def _start(self, job: ImageWebJob, login: bool):
        job.attempts += 1
        self.qr.pop(job.id, None)
        self.repository.save(job)
        self.active_id = job.id
        self.active = asyncio.create_task(self._run(job, login))

    async def _run(self, job: ImageWebJob, login: bool):
        def progress(event: ProgressEvent):
            if event.task_id:
                job.task_id = event.task_id
            job.phase = "downloading" if event.phase == "images" else "collecting"
            job.completed, job.total = event.completed, event.total
            self.repository.save(job)

        def on_qr(content: bytes):
            version = self.qr.get(job.id, (0, b""))[0] + 1
            self.qr[job.id] = (version, content)
            job.phase = "waiting_login"
            self.repository.save(job)

        try:
            job.phase = "collecting"
            self.repository.save(job)
            job.report = await self.executor.execute(job, progress, on_qr, login)
            results = job.report.get("results", [])
            job.error_code = next((r.get("error_code") for r in results if r.get("error_code")), None)
            if any(r["status"] == "restricted" for r in results):
                job.phase = "restricted"
            else:
                job.phase = {"succeeded": "completed", "failed": "failed"}.get(job.report["status"], "partial")
        except asyncio.CancelledError:
            job.phase = "cancelled"
            raise
        except CollectionError as exc:
            job.error_code = exc.code
            job.phase = "restricted" if exc.code in ("login_required", "access_restricted", "login_timeout", "qr_unavailable") else "failed"
        except Exception:
            job.phase, job.error_code = "failed", "unexpected_job_error"
        finally:
            self.qr.pop(job.id, None)
            self.repository.save(job)

    async def cancel(self, job_id: str):
        self.repository.get(job_id)
        if self.active_id == job_id and self.active is not None and not self.active.done():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)

    async def close(self):
        if self.active is not None and not self.active.done():
            self.active.cancel()
            await asyncio.gather(self.active, return_exceptions=True)

    def snapshot(self, job_id: str) -> dict:
        job = self.repository.get(job_id)
        images = []
        for result in (job.report or {}).get("results", []):
            if result["capability"] == "images":
                for item in result.get("assets", []):
                    images.append({"status": item["status"], "error_code": item.get("error_code"),
                                   "downloaded": bool(item.get("local_path")), "url": item["asset"]["url"]})
        return {"id": job.id, "url": job.source_url, "phase": job.phase,
                "completed": job.completed, "total": job.total, "error_code": job.error_code,
                "attempts": job.attempts, "images": images,
                "qr_version": self.qr.get(job_id, (0, b""))[0], "task_id": job.task_id}
