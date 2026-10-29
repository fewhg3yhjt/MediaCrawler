# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\text.py
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

"""Post text parsing and collection, independent from sources and presentation."""

from html.parser import HTMLParser
from dataclasses import replace

from .models import CollectionError, CollectionResult, PostRef, PostText, ProgressEvent, TextOptions
from .ports import LongTextSource, ProgressObserver, TextResolver
from .adapters import RequestGate


class _VisibleText(HTMLParser):
    _break_tags = {"br", "p", "div", "li", "blockquote"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self._ignored = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._ignored += 1
        elif not self._ignored and tag in self._break_tags:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self._ignored:
            self._ignored -= 1
        elif not self._ignored and tag in self._break_tags:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._ignored:
            self.parts.append(data)


def visible_text(value: str) -> str:
    parser = _VisibleText()
    parser.feed(value)
    parser.close()
    lines = [line.replace("\u200b", "").strip() for line in "".join(parser.parts).splitlines()]
    return "\n".join(line for line in lines if line).strip()


class WeiboTextResolver:
    """Adapt one Weibo detail response into a body with explicit completeness."""

    def __init__(self, client, gate: RequestGate, long_text_source: LongTextSource | None = None):
        self.client = client
        self.gate = gate
        self.long_text_source = long_text_source

    async def resolve(self, post: PostRef, options: TextOptions) -> PostText:
        if post.text is not None and (post.text.complete or not options.require_full_text):
            return post.text
        await self.gate.wait()
        try:
            response = await self.client.get_note_info_by_id(post.note_id)
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("detail_request_failed") from exc
        raw = response.get("mblog") if isinstance(response, dict) else None
        if not isinstance(raw, dict) or str(raw.get("id")) != post.note_id:
            raise CollectionError("detail_unavailable")
        if raw.get("deleted") or raw.get("state") == 7:
            raise CollectionError("detail_unavailable")

        long_text_error = None
        long_text_fetched = False
        if raw.get("isLongText") and not raw.get("longTextContent") and self.long_text_source is not None:
            try:
                await self.gate.wait()
                full_response = await self.long_text_source.get_full_text_by_id(post.note_id)
                full_raw = full_response.get("mblog") if isinstance(full_response, dict) else None
                if not isinstance(full_raw, dict) or str(full_raw.get("id")) != post.note_id:
                    raise CollectionError("detail_unavailable")
                raw = full_raw
                long_text_fetched = True
            except CollectionError as exc:
                if exc.code in ("login_required", "access_restricted", "detail_unavailable"):
                    raise
                long_text_error = "full_text_unavailable"
            except Exception:
                long_text_error = "full_text_unavailable"

        long_html = raw.get("longTextContent")
        body_html = long_html or raw.get("text") or ""
        if not isinstance(body_html, str):
            raise CollectionError("invalid_text_response")
        text_raw = raw.get("text_raw")
        if isinstance(long_html, str) and long_html:
            text_raw = visible_text(long_html)
        elif not isinstance(text_raw, str) or not text_raw:
            text_raw = visible_text(body_html)
        else:
            text_raw = text_raw.replace("\u200b", "").strip()
        complete = long_text_fetched or not bool(raw.get("isLongText")) or bool(raw.get("longTextContent"))
        if not body_html and text_raw:
            body_html = text_raw
        if len(text_raw) > 1_000_000 or len(body_html) > 1_000_000:
            raise CollectionError("text_too_large")
        return PostText(post.note_id, text_raw, body_html, complete, long_text_error)


class TextCollector:
    name = "text"
    options_type = TextOptions

    def __init__(self, resolver: TextResolver, observer: ProgressObserver | None = None):
        self.resolver = resolver
        self.observer = observer

    async def collect(self, post: PostRef, options: TextOptions, policy: str) -> CollectionResult:
        if self.observer:
            self.observer(ProgressEvent("text", post_id=post.note_id, capability=self.name, total=1))
        try:
            target = replace(post, text=None) if policy == "refresh" else post
            content = await self.resolver.resolve(target, options)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)

        if not content.text:
            status = "no_content" if content.complete else "partial"
            error_code = None if content.complete else "full_text_unavailable"
        elif options.require_full_text and not content.complete:
            status, error_code = "partial", content.error_code or "full_text_unavailable"
        else:
            status, error_code = "succeeded", None
        if self.observer:
            self.observer(ProgressEvent("text", post_id=post.note_id, capability=self.name, completed=1, total=1))
        return CollectionResult(status, error_code=error_code, content=content)
