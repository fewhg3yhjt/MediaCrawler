# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\service.py
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

"""Planning validates all capabilities before execution or source discovery."""

import asyncio
import json
from dataclasses import dataclass

from pydantic import BaseModel

from .models import CollectionError, CollectionResult, CrawlRequest, TaskExecutionError, ProgressEvent
from .ports import Collector, PostSource, TaskRepository, ProgressObserver


def validate_capabilities(request: CrawlRequest, types: dict[str, type[BaseModel]]) -> dict[str, BaseModel]:
    unsupported = request.collect.keys() - types.keys()
    if unsupported:
        raise ValueError(f"unsupported capabilities: {', '.join(sorted(unsupported))}")
    return {name: types[name].model_validate(options) for name, options in request.collect.items()}


@dataclass(frozen=True)
class CollectionStep:
    collector: Collector
    options: BaseModel


@dataclass(frozen=True)
class CollectionPlan:
    request_json: str
    steps: tuple[CollectionStep, ...]
    policy: str
    max_posts: int
    collection: str


class CollectionPlanner:
    def __init__(self, collectors: tuple[Collector, ...]):
        self.collectors = {collector.name: collector for collector in collectors}
        if len(self.collectors) != len(collectors):
            raise ValueError("duplicate collector registration")

    def plan(self, request: CrawlRequest) -> CollectionPlan:
        options = validate_capabilities(request, {name: c.options_type for name, c in self.collectors.items()})
        steps = tuple(
            CollectionStep(self.collectors[name], parsed) for name, parsed in options.items()
        )
        collection = request.output.collection or (
            request.source.collection if request.source.type == "archive" else "manual"
        )
        return CollectionPlan(
            json.dumps(request.model_dump(mode="json"), sort_keys=True, ensure_ascii=False),
            steps, request.update.default, request.limits.max_posts, collection,
        )


class TaskRunner:
    def __init__(self, repository: TaskRepository, scope: str, observer: ProgressObserver | None = None):
        self.repository = repository
        self.scope = scope
        self.observer = observer

    async def run(self, plan: CollectionPlan, source: PostSource, resume_task: str | None = None) -> dict:
        if resume_task:
            self.repository.resume(resume_task, plan.request_json, self.scope)
            task_id = resume_task
        else:
            task_id = self.repository.create(plan.request_json, self.scope, plan.collection)
        if self.observer:
            self.observer(ProgressEvent("task_started", task_id=task_id))

        async def collect(post):
            for step in plan.steps:
                if resume_task and self.repository.completed(task_id, post, step.collector.name):
                    continue
                if self.observer:
                    self.observer(ProgressEvent("collecting", task_id, post.note_id, step.collector.name))
                try:
                    result = await step.collector.collect(post, step.options, plan.policy)
                except Exception:
                    # Persist an isolated failure without leaking cookies or raw URLs.
                    result = CollectionResult("failed", error_code="unexpected_collector_error")
                self.repository.record(task_id, post, step.collector.name, step.options.model_dump_json(), result)

        try:
            seen = set()
            # Durable discovered work is processed before replaying the source.
            for post in self.repository.pending(task_id):
                seen.add(post.note_id)
                await collect(post)
            stop_reason = "source_exhausted"
            async for post in source.discover():
                if post.note_id in seen:
                    continue
                if len(seen) >= plan.max_posts:
                    stop_reason = "limit_reached"
                    break
                self.repository.discover(task_id, post)
                seen.add(post.note_id)
                await collect(post)
            report = self.repository.report(task_id)
            incomplete = any(item["status"] not in ("succeeded", "no_content") for item in report["capabilities"])
            all_failed = bool(report["capabilities"]) and all(
                item["status"] in ("failed", "restricted") for item in report["capabilities"]
            )
            self.repository.finish(task_id, "failed" if all_failed else "partial" if incomplete else "succeeded", stop_reason)
        except asyncio.CancelledError:
            self.repository.finish(task_id, "cancelled", "cancelled")
            raise
        except Exception as exc:
            code = exc.code if isinstance(exc, CollectionError) else "source_or_storage_failed"
            self.repository.finish(task_id, "failed", code)
            raise TaskExecutionError(task_id, code) from exc
        return self.repository.report(task_id)
