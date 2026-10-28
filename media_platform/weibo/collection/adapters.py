# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\adapters.py
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

"""Weibo response adaptation and bounded, validated HTTP image downloads."""

import asyncio
import io
import math
import time
from typing import Protocol

import httpx
from PIL import Image, UnidentifiedImageError

from .models import (
    CollectionError, DownloadedAsset, MediaAsset, ImageManifest, ImageOptions, PostRef,
)
from .ports import AssetValidator, BinaryTransport
from .sources import image_asset


class DetailClient(Protocol):
    async def get_note_info_by_id(self, note_id: str) -> dict: ...


class CachedDetailClient:
    """Reuse a detail response across selected capabilities within one task."""

    def __init__(self, client: DetailClient):
        self.client = client
        self._responses = {}
        self._failures = {}

    async def get_note_info_by_id(self, note_id: str) -> dict:
        if note_id in self._failures:
            raise self._failures[note_id]
        if note_id not in self._responses:
            try:
                self._responses[note_id] = await self.client.get_note_info_by_id(note_id)
            except Exception as exc:
                self._failures[note_id] = exc
                raise
        return self._responses[note_id]

    async def get_full_text_by_id(self, note_id: str) -> dict:
        method = getattr(self.client, "get_full_text_by_id", None)
        if method is None:
            raise CollectionError("full_text_unavailable")
        key = f"full:{note_id}"
        if key in self._failures:
            raise self._failures[key]
        if key not in self._responses:
            try:
                self._responses[key] = await method(note_id)
            except Exception as exc:
                self._failures[key] = exc
                raise
        return self._responses[key]


class RequestGate:
    """One rate limit shared by detail and file requests in this process."""

    def __init__(self, interval: float = 2.0):
        if not math.isfinite(interval) or interval < 0:
            raise ValueError("interval must be nonnegative")
        self.interval = interval
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def wait(self):
        async with self._lock:
            await asyncio.sleep(max(0.0, self._next - time.monotonic()))
            self._next = time.monotonic() + self.interval


def _extract(post_id: str, raw: dict) -> ImageManifest:
    assets = []

    def extract_owner(owner: dict) -> bool:
        owner_id = str(owner.get("id") or "")
        if not owner_id:
            return False
        pics = owner.get("pics")
        infos = owner.get("pic_infos") or {}
        if pics is None and infos:
            ids = owner.get("pic_ids") or list(infos)
            pics = [infos[pid] for pid in ids if pid in infos]
        if pics is not None and not isinstance(pics, list):
            raise CollectionError("invalid_image_response")
        parsed = 0
        for pic in pics or []:
            if isinstance(pic, str):
                url, pid = pic, ""
            elif isinstance(pic, dict):
                large = pic.get("largest") or pic.get("large") or {}
                url, pid = large.get("url") or pic.get("url"), str(pic.get("pid") or "")
            else:
                continue
            if url:
                assets.append(image_asset(post_id, owner_id, url, pid))
                parsed += 1
        expected = len(owner.get("pic_ids") or [])
        expected = max(expected, int(owner.get("pic_num") or 0))
        known = any(key in owner for key in ("pics", "pic_infos", "pic_ids", "pic_num"))
        return known and parsed >= expected and parsed == len(pics or []) and not owner.get("mix_media_info")

    originals = extract_owner(raw)
    rt = raw.get("retweeted_status")
    retweeted = extract_owner(rt) if isinstance(rt, dict) else rt is None
    return ImageManifest(tuple(assets), originals, retweeted)


class WeiboImageResolver:
    def __init__(self, client: DetailClient, gate: RequestGate):
        self.client = client
        self.gate = gate

    async def resolve(self, post: PostRef, options: ImageOptions) -> ImageManifest:
        if post.images is not None:
            available = any(
                options.include_retweeted or a.owner_post_id == post.note_id
                for a in post.images.assets
            )
            if post.images.satisfies(options) or (available and not options.verify_manifest):
                return post.images
        await self.gate.wait()
        try:
            response = await self.client.get_note_info_by_id(post.note_id)
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("detail_request_failed") from exc
        raw = response.get("mblog")
        if not isinstance(raw, dict) or str(raw.get("id")) != post.note_id:
            raise CollectionError("detail_unavailable")
        if raw.get("deleted") or raw.get("state") == 7:
            raise CollectionError("detail_unavailable")
        try:
            return _extract(post.note_id, raw)
        except (ValueError, TypeError, AttributeError) as exc:
            raise CollectionError("invalid_image_response") from exc


class HttpBinaryTransport:
    def __init__(self, client: httpx.AsyncClient, gate: RequestGate, max_bytes: int = 25 * 1024 * 1024):
        self.client = client
        self.gate = gate
        self.max_bytes = max_bytes

    async def fetch(self, url: str) -> bytes:
        await self.gate.wait()
        try:
            async with self.client.stream(
                "GET", url, headers={"Referer": "https://weibo.com/"},
                follow_redirects=True,
            ) as response:
                response.raise_for_status()
                chunks, length = [], 0
                async for chunk in response.aiter_bytes():
                    length += len(chunk)
                    if length > self.max_bytes:
                        raise CollectionError("asset_too_large")
                    chunks.append(chunk)
        except httpx.HTTPError as exc:
            raise CollectionError("asset_request_failed") from exc
        return b"".join(chunks)


class ImageValidator:
    def validate(self, content: bytes) -> DownloadedAsset:
        try:
            with Image.open(io.BytesIO(content)) as image:
                extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp"}.get(image.format)
                image.verify()
            if extension is None:
                raise CollectionError("unsupported_image_format")
            with Image.open(io.BytesIO(content)) as decoded:
                decoded.seek(getattr(decoded, "n_frames", 1) - 1)
                decoded.load()
        except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError) as exc:
            raise CollectionError("invalid_image_content") from exc
        return DownloadedAsset(content, extension)


class ValidatedAssetDownloader:
    """Shared download algorithm; media-specific validation is a strategy."""

    def __init__(self, transport: BinaryTransport, validator: AssetValidator):
        self.transport = transport
        self.validator = validator

    async def download(self, asset: MediaAsset) -> DownloadedAsset:
        return self.validator.validate(await self.transport.fetch(asset.url))
