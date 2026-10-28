# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\web_app.py
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

"""Local HTTP boundary for the image application, with explicit session consent."""

import asyncio
import mimetypes
import secrets
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from pydantic import Field, StrictBool, field_validator, model_validator
from starlette.middleware.trustedhost import TrustedHostMiddleware

from .links import WeiboPostLink
from .models import CollectionError, Contract, CrawlRequest
from .presentation import ImageArchiveWriter
from .web_jobs import ImageJobController


class SubmitImageJob(Contract):
    url: str = Field(min_length=1, max_length=2048)
    download: StrictBool = True
    include_images: StrictBool = True
    include_text: StrictBool = False
    include_video: StrictBool = False
    include_audio: StrictBool = False
    include_retweeted: StrictBool = False
    use_browser_session: StrictBool = False

    @field_validator("url")
    @classmethod
    def valid_link(cls, value):
        WeiboPostLink.parse(value)
        return value.strip()

    @model_validator(mode="after")
    def at_least_one_capability(self):
        if not (self.include_images or self.include_text or self.include_video or self.include_audio):
            raise ValueError("select_at_least_one_capability")
        return self


class RetryImageJob(Contract):
    login: StrictBool = False
    consent: StrictBool = False


def create_image_app(controller: ImageJobController, workspace: Path) -> FastAPI:
    workspace = workspace.resolve()
    token = secrets.token_urlsafe(32)
    template = Path(__file__).with_name("static") / "images.html"

    @asynccontextmanager
    async def lifespan(_):
        yield
        await controller.close()

    app = FastAPI(title="微博图片采集", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "testserver"])
    app.state.controller = controller
    app.state.csrf_token = token

    @app.middleware("http")
    async def local_requests(request: Request, call_next):
        if request.method not in ("GET", "HEAD") and not secrets.compare_digest(
            request.headers.get("X-Collection-Token", ""), token,
        ):
            return JSONResponse({"detail": "local_request_token_required"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        return response

    @app.exception_handler(CollectionError)
    async def collection_error(_, exc):
        code = 409 if exc.code in ("another_job_running", "job_already_completed") else 400
        return JSONResponse({"detail": exc.code}, status_code=code)

    @app.exception_handler(KeyError)
    async def missing_job(_, exc):
        return JSONResponse({"detail": "job_not_found"}, status_code=404)

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return template.read_text(encoding="utf-8").replace("__COLLECTION_TOKEN__", token)

    @app.get("/api/jobs")
    async def jobs():
        return [controller.snapshot(job.id) for job in controller.repository.list()]

    @app.post("/api/jobs", status_code=202)
    async def submit(body: SubmitImageJob):
        collect = {}
        if body.include_images:
            collect["images"] = {"download": body.download, "include_retweeted": body.include_retweeted}
        if body.include_text:
            collect["text"] = {}
        if body.include_video:
            collect["video"] = {"include_retweeted": body.include_retweeted}
        if body.include_audio:
            collect["audio"] = {"include_retweeted": body.include_retweeted}
        request = CrawlRequest.model_validate({
            "source": {"type": "detail", "note_ids": [body.url]},
            "collect": collect,
        })
        job = controller.submit(request.model_dump(mode="json"), body.url, body.use_browser_session)
        return controller.snapshot(job.id)

    @app.get("/api/jobs/{job_id}")
    async def job_status(job_id: str):
        return controller.snapshot(job_id)

    @app.post("/api/jobs/{job_id}/retry", status_code=202)
    async def retry(job_id: str, body: RetryImageJob):
        controller.retry(job_id, body.login, body.consent)
        return controller.snapshot(job_id)

    @app.post("/api/jobs/{job_id}/cancel")
    async def cancel(job_id: str):
        await controller.cancel(job_id)
        return controller.snapshot(job_id)

    @app.get("/api/jobs/{job_id}/qr")
    async def qr(job_id: str):
        controller.repository.get(job_id)
        value = controller.qr.get(job_id)
        if value is None:
            raise HTTPException(404, "qr_not_available")
        return Response(value[1], media_type="image/png")

    def assets(job_id: str, kind: str):
        job = controller.repository.get(job_id)
        capability = {"images": "images", "video": "video", "audio": "audio"}.get(kind)
        if capability is None:
            raise HTTPException(404, "media_kind_not_found")
        return [a for r in (job.report or {}).get("results", [])
                if r["capability"] == capability for a in r.get("assets", [])]

    def checked_path(item, kind: str):
        if item.get("status") != "succeeded" or not item.get("local_path"):
            raise HTTPException(404, "media_not_downloaded")
        path = Path(item["local_path"]).resolve()
        root = (workspace / kind).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise HTTPException(404, "media_file_missing")
        return path

    @app.get("/api/jobs/{job_id}/images/{index}")
    async def image(job_id: str, index: int, download: bool = False):
        items = assets(job_id, "images")
        if index < 0 or index >= len(items):
            raise HTTPException(404, "image_not_found")
        path = checked_path(items[index], "images")
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0],
                            filename=f"weibo-{index + 1}{path.suffix}" if download else None)

    @app.get("/api/jobs/{job_id}/media/{kind}/{index}")
    async def media(job_id: str, kind: str, index: int, download: bool = False):
        if kind not in ("video", "audio"):
            raise HTTPException(404, "media_kind_not_found")
        items = assets(job_id, kind)
        if index < 0 or index >= len(items):
            raise HTTPException(404, "media_not_found")
        path = checked_path(items[index], kind)
        return FileResponse(path, media_type=mimetypes.guess_type(path.name)[0],
                            filename=f"weibo-{kind}-{index + 1}{path.suffix}" if download else None)

    @app.get("/api/jobs/{job_id}/archive")
    async def archive(job_id: str):
        job = controller.repository.get(job_id)
        downloaded = [
            item for kind in ("images", "video", "audio") for item in assets(job_id, kind)
            if item.get("local_path") and item["status"] == "succeeded"
        ]
        if not downloaded:
            raise HTTPException(409, "no_downloaded_media")
        for kind in ("images", "video", "audio"):
            for item in assets(job_id, kind):
                if item.get("local_path") and item["status"] == "succeeded":
                    checked_path(item, kind)
        path = await asyncio.to_thread(ImageArchiveWriter(workspace / "exports").write, job.report)
        return FileResponse(path, media_type="application/zip", filename=f"weibo-{job_id[:8]}-media.zip")

    return app
