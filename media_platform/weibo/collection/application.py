# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\application.py
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

"""Shared application use case for CLI and web; no browser or HTTP lifecycle."""

from pathlib import Path

from .models import CrawlRequest
from .presentation import ImageReportWriter
from .service import CollectionPlanner, TaskRunner
from .sources import ArchiveSource, CreatorSource, DetailSource


class PostSourceFactory:
    def __init__(self, archive_directory: Path, creator_client=None, request_gate=None):
        self.archive_directory = archive_directory
        self.creator_client = creator_client
        self.request_gate = request_gate
        self.factories = {
            "detail": lambda spec: DetailSource(spec.note_ids),
            "archive": lambda spec: ArchiveSource(self.archive_directory, spec.collection, spec.note_ids),
        }

    def create(self, spec, max_posts: int = 1000):
        if spec.type == "creator":
            return self._creator(spec, max_posts)
        return self.factories[spec.type](spec)

    def _creator(self, spec, max_posts: int):
        if self.creator_client is None or self.request_gate is None:
            raise ValueError("creator source dependencies are unavailable")
        return CreatorSource(spec.creator_id, self.creator_client, self.request_gate, max_pages=max_posts + 2)


class CollectionApplication:
    def __init__(self, planner: CollectionPlanner, runner: TaskRunner,
                 sources: PostSourceFactory, reports: ImageReportWriter):
        self.planner = planner
        self.runner = runner
        self.sources = sources
        self.reports = reports

    async def execute(self, request: CrawlRequest, resume_task: str | None = None) -> dict:
        plan = self.planner.plan(request)
        report = await self.runner.run(plan, self.sources.create(request.source, plan.max_posts), resume_task)
        report["outputs"] = self.reports.write(report)
        return report
