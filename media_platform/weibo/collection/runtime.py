# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\runtime.py
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

"""Production dependency composition for one explicit web image job."""

from contextlib import AsyncExitStack
from pathlib import Path

import httpx

from .adapters import (
    AudioFileValidator, CachedDetailClient, HttpBinaryTransport, ImageValidator, RequestGate,
    StreamingAssetDownloader, ValidatedAssetDownloader, VideoFileValidator, WeiboImageResolver,
)
from .application import CollectionApplication, PostSourceFactory
from .comments import CommentsCollector, WeiboHotCommentsResolver
from .images import ImageCollector
from .login import QrLoginFlow
from .media import MediaCollector, WeiboMediaResolver
from .models import AudioOptions, CrawlRequest, VideoOptions
from .presentation import ImageReportWriter
from .repository import FileAssetRepository, SqliteTaskRepository
from .service import CollectionPlanner, TaskRunner, validate_capabilities
from .session import BrowserProfileSession, BrowserWeiboDetailClient, HttpWeiboDetailClient
from .text import TextCollector, WeiboTextResolver
from .web_jobs import ImageWebJob


class WebImageExecutor:
    def __init__(self, workspace: Path, profile: Path, account_scope: str,
                 interval: float = 2, login_timeout: float = 180):
        self.workspace = workspace.resolve()
        self.profile = profile.resolve()
        self.account_scope = account_scope
        self.interval = interval
        self.login_timeout = login_timeout

    async def execute(self, job: ImageWebJob, progress, on_qr, login: bool) -> dict:
        request = CrawlRequest.model_validate(job.request)
        validate_capabilities(request, {
            ImageCollector.name: ImageCollector.options_type,
            TextCollector.name: TextCollector.options_type,
            "video": VideoOptions,
            "audio": AudioOptions,
            CommentsCollector.name: CommentsCollector.options_type,
        })
        scope = self.account_scope if job.use_browser else "public"
        if job.scope != scope:
            job.task_id = None
        job.scope = scope
        async with AsyncExitStack() as stack:
            media_http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
            if job.use_browser:
                context = await stack.enter_async_context(BrowserProfileSession(self.profile, create=True).context())
                if login:
                    login_flow = QrLoginFlow(self.login_timeout)
                    if request.source.type == "creator":
                        await login_flow.ensure_creator(context, request.source.creator_id, on_qr)
                    elif request.source.type == "detail":
                        await login_flow.ensure(context, request.source.note_ids[0], on_qr)
                    elif request.source.type == "self_feed":
                        await login_flow.ensure_self_feed(context, request.source.feed_type, on_qr)
                client = BrowserWeiboDetailClient(context.request)
            else:
                detail_http = await stack.enter_async_context(httpx.AsyncClient(timeout=30, headers={"User-Agent": "Mozilla/5.0"}))
                client = HttpWeiboDetailClient(detail_http)
            client = CachedDetailClient(client)
            gate = RequestGate(self.interval)
            collector = ImageCollector(
                WeiboImageResolver(client, gate),
                ValidatedAssetDownloader(HttpBinaryTransport(media_http, gate), ImageValidator()),
                FileAssetRepository(self.workspace / "images"), scope, progress,
            )
            text = TextCollector(WeiboTextResolver(client, gate, client), progress)
            video = MediaCollector(
                "video", WeiboMediaResolver(client, gate, "video"),
                StreamingAssetDownloader(HttpBinaryTransport(media_http, gate), VideoFileValidator(),
                                         self.workspace / "video", 1024 * 1024 * 1024),
                FileAssetRepository(self.workspace / "video"), scope, VideoOptions, progress,
            )
            audio = MediaCollector(
                "audio", WeiboMediaResolver(client, gate, "audio"),
                StreamingAssetDownloader(HttpBinaryTransport(media_http, gate), AudioFileValidator(),
                                         self.workspace / "audio", 100 * 1024 * 1024),
                FileAssetRepository(self.workspace / "audio"), scope, AudioOptions, progress,
            )
            comments = CommentsCollector(WeiboHotCommentsResolver(client, gate, observer=progress), progress)
            application = CollectionApplication(
                CollectionPlanner((collector, text, video, audio, comments)),
                TaskRunner(SqliteTaskRepository(self.workspace / "tasks.sqlite3"), scope, progress),
                PostSourceFactory(self.workspace / "jsonl", client, gate),
                ImageReportWriter(self.workspace / "reports"),
            )
            return await application.execute(request, job.task_id)
