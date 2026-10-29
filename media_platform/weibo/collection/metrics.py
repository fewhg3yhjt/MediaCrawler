# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\metrics.py
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

"""Snapshot post interaction counts as an independent collection capability."""

from datetime import datetime, timezone

from .models import CollectionError, CollectionResult, MetricsOptions, PostMetrics, PostRef, ProgressEvent
from .ports import MetricsResolver, ProgressObserver


def metrics_from_status(raw: dict, observed_at: str | None = None) -> PostMetrics | None:
    """Best-effort normalize counts already present in a Weibo status response."""
    if not isinstance(raw, dict):
        return None

    def count(*keys):
        for key in keys:
            value = raw.get(key)
            if value is None or isinstance(value, bool):
                continue
            if isinstance(value, int) and value >= 0:
                return value
            if isinstance(value, str) and value.isascii() and value.isdigit():
                return int(value)
        return None

    values = (
        count("attitudes_count", "like_count", "liked_count"),
        count("comments_count", "comment_count"),
        count("reposts_count", "repost_count", "shared_count", "share_count"),
    )
    if all(value is None for value in values):
        return None
    return PostMetrics(
        like_count=values[0], comment_count=values[1], repost_count=values[2],
        observed_at=observed_at,
        complete=all(value is not None for value in values),
    )


class WeiboMetricsResolver:
    """Reuse a complete source snapshot or request the post detail on demand."""

    def __init__(self, client, gate):
        self.client = client
        self.gate = gate

    async def resolve(self, post: PostRef, options: MetricsOptions, policy: str) -> PostMetrics:
        snapshot = post.metrics
        if snapshot is not None and snapshot.complete and policy != "refresh":
            return snapshot

        await self.gate.wait()
        try:
            response = await self.client.get_note_info_by_id(post.note_id)
        except CollectionError:
            if snapshot is not None and policy != "refresh":
                return snapshot
            raise
        except Exception as exc:
            if snapshot is not None and policy != "refresh":
                return snapshot
            raise CollectionError("metrics_request_failed") from exc

        raw = response.get("mblog") if isinstance(response, dict) else None
        if not isinstance(raw, dict) or str(raw.get("id")) != post.note_id:
            raise CollectionError("detail_unavailable")
        if raw.get("deleted") or raw.get("state") == 7:
            raise CollectionError("detail_unavailable")
        metrics = metrics_from_status(raw, datetime.now(timezone.utc).isoformat(timespec="seconds"))
        if metrics is None:
            raise CollectionError("metrics_unavailable")
        return metrics


class MetricsCollector:
    name = "metrics"
    options_type = MetricsOptions

    def __init__(self, resolver: MetricsResolver, observer: ProgressObserver | None = None):
        self.resolver = resolver
        self.observer = observer

    async def collect(self, post: PostRef, options: MetricsOptions, policy: str) -> CollectionResult:
        if self.observer:
            self.observer(ProgressEvent("metrics", post_id=post.note_id,
                                        capability=self.name, total=1))
        try:
            snapshot = await self.resolver.resolve(post, options, policy)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)

        status = "succeeded" if snapshot.complete else "partial"
        error_code = None if snapshot.complete else "metrics_incomplete"
        if self.observer:
            self.observer(ProgressEvent("metrics", post_id=post.note_id, capability=self.name,
                                        completed=1, total=1))
        return CollectionResult(status, error_code=error_code, metrics=snapshot)
