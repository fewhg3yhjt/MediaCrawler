# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\likers.py
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

"""Bounded liker-list collection, independent from interaction metrics."""

from .models import (
    CollectionError,
    CollectionResult,
    LikerBatch,
    LikerRecord,
    LikersOptions,
    PostRef,
    ProgressEvent,
)
from .ports import LikerPageClient, ProgressObserver


class WeiboLikersResolver:
    """Read a bounded list of users who liked a post, guarding paging anomalies."""

    def __init__(self, client: LikerPageClient, gate, max_pages: int = 100,
                 observer: ProgressObserver | None = None):
        self.client = client
        self.gate = gate
        self.max_pages = max_pages
        self.observer = observer

    async def resolve(self, post: PostRef, options: LikersOptions) -> LikerBatch:
        items: list[LikerRecord] = []
        seen_ids: set[str] = set()
        page_signatures: set[tuple[str, ...]] = set()
        total_available = None

        for page in range(1, self.max_pages + 1):
            await self.gate.wait()
            try:
                response = await self.client.get_note_likers_page(post.note_id, page)
            except CollectionError:
                raise
            except Exception as exc:
                raise CollectionError("likers_request_failed") from exc

            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise CollectionError("invalid_likers_page")
            raw_items = response["items"]
            page_total = response.get("total")
            if page_total is not None:
                if type(page_total) is not int or page_total < 0:
                    raise CollectionError("invalid_likers_page")
                if page_total == 0 and response["items"]:
                    raise CollectionError("invalid_likers_page")
                total_available = page_total

            normalized = [self._normalize(item) for item in raw_items]
            signature = tuple(item.user_id for item in normalized)
            if signature and signature in page_signatures:
                raise CollectionError("likers_page_repeated")
            if signature:
                page_signatures.add(signature)

            if not normalized:
                if total_available is not None and len(items) < total_available:
                    raise CollectionError("empty_likers_page_unverified")
                return LikerBatch(tuple(items), page, True, "end", total_available)

            for liker in normalized:
                if liker.user_id in seen_ids:
                    continue
                seen_ids.add(liker.user_id)
                items.append(LikerRecord(liker.user_id, liker.user_name, len(items) + 1))

            if self.observer:
                self.observer(ProgressEvent(
                    "likers", post_id=post.note_id, capability="likers",
                    completed=len(items), total=options.max_count,
                ))

            if (total_available is not None and total_available <= options.max_count
                    and len(items) >= total_available):
                return LikerBatch(tuple(items[:total_available]), page, True, "end", total_available)
            if len(items) >= options.max_count:
                return LikerBatch(tuple(items[:options.max_count]), page, False,
                                  "limit_reached", total_available)

        raise CollectionError("likers_page_limit")

    @staticmethod
    def _normalize(raw) -> LikerRecord:
        if not isinstance(raw, dict):
            raise CollectionError("invalid_likers_page")
        user = raw.get("user") if isinstance(raw.get("user"), dict) else raw
        user_id = str(user.get("idstr") or user.get("id") or "")
        if not user_id.isascii() or not user_id.isdigit() or int(user_id) <= 0:
            raise CollectionError("invalid_likers_page")
        user_name = user.get("screen_name") or user.get("name") or ""
        if not isinstance(user_name, str):
            raise CollectionError("invalid_likers_page")
        return LikerRecord(user_id, user_name, 0)


class LikersCollector:
    name = "likers"
    options_type = LikersOptions

    def __init__(self, resolver: WeiboLikersResolver, observer: ProgressObserver | None = None):
        self.resolver = resolver
        self.observer = observer

    async def collect(self, post: PostRef, options: LikersOptions, policy: str) -> CollectionResult:
        if self.observer:
            self.observer(ProgressEvent("likers", post_id=post.note_id,
                                        capability=self.name, total=options.max_count))
        try:
            batch = await self.resolver.resolve(post, options)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)

        if not batch.items:
            status = "no_content" if batch.complete else "partial"
            error_code = None if batch.complete else "likers_limit_reached"
        elif batch.complete:
            status, error_code = "succeeded", None
        else:
            status, error_code = "partial", "likers_limit_reached"
        if self.observer:
            self.observer(ProgressEvent("likers", post_id=post.note_id, capability=self.name,
                                        completed=len(batch.items),
                                        total=len(batch.items) if batch.complete else options.max_count))
        return CollectionResult(status, error_code=error_code, liker_batch=batch)
