# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\comments.py
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

"""Bounded hotflow comment collection, separate from post discovery and media."""

from .models import (
    CollectionError, CollectionResult, CommentBatch, CommentRecord, CommentsOptions,
    PostRef, ProgressEvent,
)
from .ports import CommentPageClient, ProgressObserver
from .text import visible_text


class WeiboHotCommentsResolver:
    """Page Weibo's hotflow endpoint and guard against cursor loops or uncertain gaps."""

    def __init__(self, client: CommentPageClient, gate, max_pages: int = 600,
                 observer: ProgressObserver | None = None):
        self.client = client
        self.gate = gate
        self.max_pages = max_pages
        self.observer = observer

    async def resolve(self, post: PostRef, options: CommentsOptions) -> CommentBatch:
        items: list[CommentRecord] = []
        seen_ids: set[str] = set()
        seen_cursors: set[tuple[int, int]] = {(0, 0)}
        max_id, max_id_type, pages = 0, 0, 0

        while pages < self.max_pages:
            await self.gate.wait()
            try:
                response = await self.client.get_note_comments_page(post.note_id, max_id, max_id_type)
            except CollectionError:
                raise
            except Exception as exc:
                raise CollectionError("comments_request_failed") from exc
            if not isinstance(response, dict) or not isinstance(response.get("items"), list):
                raise CollectionError("invalid_comments_page")
            pages += 1
            next_id = self._cursor(response.get("max_id"))
            next_type = self._cursor(response.get("max_id_type"))
            raw_items = response["items"]
            if not raw_items:
                if next_id == 0:
                    return CommentBatch(tuple(items), pages, True, "end")
                raise CollectionError("empty_comments_page_unverified")

            for raw in raw_items:
                if len(items) >= options.max_count:
                    break
                comment = self._normalize(post.note_id, raw, len(items) + 1)
                if comment.comment_id in seen_ids:
                    continue
                seen_ids.add(comment.comment_id)
                items.append(comment)

            if self.observer:
                self.observer(ProgressEvent("comments", post_id=post.note_id, capability="comments",
                                            completed=len(items), total=options.max_count))

            if next_id == 0:
                return CommentBatch(tuple(items), pages, True, "end")
            cursor = (next_id, next_type)
            if cursor in seen_cursors or cursor == (max_id, max_id_type):
                raise CollectionError("comments_cursor_stalled")
            if len(items) >= options.max_count:
                return CommentBatch(tuple(items), pages, False, "limit_reached")
            seen_cursors.add(cursor)
            max_id, max_id_type = cursor

        raise CollectionError("comments_page_limit")

    @staticmethod
    def _cursor(value) -> int:
        try:
            result = int(value)
        except (TypeError, ValueError) as exc:
            raise CollectionError("invalid_comments_page") from exc
        if result < 0:
            raise CollectionError("invalid_comments_page")
        return result

    @classmethod
    def _normalize(cls, post_id: str, raw, rank: int) -> CommentRecord:
        if not isinstance(raw, dict):
            raise CollectionError("invalid_comments_page")
        comment_id = str(raw.get("idstr") or raw.get("id") or "")
        if not comment_id or not comment_id.isascii() or not comment_id.isdigit():
            raise CollectionError("invalid_comments_page")
        user = raw.get("user") if isinstance(raw.get("user"), dict) else {}
        user_id = str(user.get("idstr") or user.get("id") or "")
        user_name = str(user.get("screen_name") or user.get("name") or "")
        body = raw.get("text") or raw.get("text_raw") or ""
        if not isinstance(body, str):
            raise CollectionError("invalid_comments_page")
        try:
            likes = max(0, int(raw.get("like_count") or raw.get("like_counts") or 0))
        except (TypeError, ValueError):
            likes = 0
        return CommentRecord(
            comment_id=comment_id, user_id=user_id, user_name=user_name,
            text=visible_text(body), created_at=str(raw.get("created_at") or ""),
            like_count=likes, rank=rank,
        )


class CommentsCollector:
    name = "comments"
    options_type = CommentsOptions

    def __init__(self, resolver: WeiboHotCommentsResolver, observer: ProgressObserver | None = None):
        self.resolver = resolver
        self.observer = observer

    async def collect(self, post: PostRef, options: CommentsOptions, policy: str) -> CollectionResult:
        if self.observer:
            self.observer(ProgressEvent("comments", post_id=post.note_id,
                                        capability=self.name, total=options.max_count))
        try:
            batch = await self.resolver.resolve(post, options)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)

        complete = batch.complete
        if not batch.items:
            status = "no_content" if complete else "partial"
            error_code = None if complete else "comments_limit_reached"
        elif complete:
            status, error_code = "succeeded", None
        else:
            status, error_code = "partial", "comments_limit_reached"
        if self.observer:
            self.observer(ProgressEvent("comments", post_id=post.note_id, capability=self.name,
                                        completed=len(batch.items),
                                        total=len(batch.items) if batch.complete else options.max_count))
        return CollectionResult(status, error_code=error_code, comment_batch=batch)
