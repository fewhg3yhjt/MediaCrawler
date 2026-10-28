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
import os
import tempfile
import time
from pathlib import Path
from typing import Protocol
from urllib.parse import urljoin, urlsplit

import httpx
from PIL import Image, UnidentifiedImageError

from .models import (
    CollectionError, DownloadedAsset, MediaAsset, ImageManifest, ImageOptions, PostRef,
)
from .ports import AssetValidator, BinaryTransport
from .sources import image_asset, image_manifest


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

    async def get_creator_notes_page(self, creator_id: str, page: int) -> dict:
        method = getattr(self.client, "get_creator_notes_page", None)
        if method is None:
            raise CollectionError("creator_request_failed")
        key = f"creator:{creator_id}:{page}"
        if key in self._failures:
            raise self._failures[key]
        if key not in self._responses:
            try:
                self._responses[key] = await method(creator_id, page)
            except Exception as exc:
                self._failures[key] = exc
                raise
        return self._responses[key]

    async def get_note_comments_page(self, note_id: str, max_id: int, max_id_type: int) -> dict:
        method = getattr(self.client, "get_note_comments_page", None)
        if method is None:
            raise CollectionError("comments_request_failed")
        key = f"comments:{note_id}:{max_id}:{max_id_type}"
        if key in self._failures:
            raise self._failures[key]
        if key not in self._responses:
            try:
                self._responses[key] = await method(note_id, max_id, max_id_type)
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
            return image_manifest(post.note_id, raw)
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

    @staticmethod
    def _media_host_allowed(url: str) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        trusted = ("weibocdn.com", "sinaimg.cn", "weibo.cn")
        return (
            parsed.scheme == "https" and parsed.username is None and parsed.password is None
            and parsed.port in (None, 443)
            and any(host == domain or host.endswith("." + domain) for domain in trusted)
        )

    async def fetch_to_file(self, url: str, path: Path, max_bytes: int) -> int:
        current = url
        try:
            for _ in range(6):
                if not self._media_host_allowed(current):
                    raise CollectionError("unsupported_media_host")
                await self.gate.wait()
                async with self.client.stream(
                    "GET", current,
                    headers={"Referer": "https://weibo.com/"}, follow_redirects=False,
                ) as response:
                    if response.status_code in (301, 302, 303, 307, 308):
                        location = response.headers.get("location")
                        if not location:
                            raise CollectionError("asset_request_failed")
                        current = urljoin(current, location)
                        continue
                    response.raise_for_status()
                    declared = response.headers.get("content-length")
                    if declared is not None:
                        try:
                            if int(declared) > max_bytes:
                                raise CollectionError("asset_too_large")
                        except ValueError as exc:
                            raise CollectionError("asset_request_failed") from exc
                    total = 0
                    with path.open("wb") as stream:
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > max_bytes:
                                raise CollectionError("asset_too_large")
                            stream.write(chunk)
                        stream.flush()
                        os.fsync(stream.fileno())
                    if total == 0:
                        raise CollectionError("invalid_asset_content")
                    return total
            raise CollectionError("too_many_asset_redirects")
        except CollectionError:
            path.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            path.unlink(missing_ok=True)
            raise
        except httpx.HTTPError as exc:
            path.unlink(missing_ok=True)
            raise CollectionError("asset_request_failed") from exc
        except OSError as exc:
            path.unlink(missing_ok=True)
            raise CollectionError("asset_write_failed") from exc


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


class VideoFileValidator:
    """Check a media container signature without buffering a full video in RAM."""

    def validate_file(self, path: Path) -> str:
        with path.open("rb") as stream:
            header = stream.read(64)
        if len(header) >= 12 and header[4:8] == b"ftyp":
            return "mp4"
        if header.startswith(b"\x1a\x45\xdf\xa3"):
            return "webm"
        if header.startswith(b"FLV"):
            return "flv"
        raise CollectionError("invalid_video_content")


class AudioFileValidator:
    """Check common audio container/frame signatures without loading the file."""

    def validate_file(self, path: Path) -> str:
        with path.open("rb") as stream:
            header = stream.read(64)
        if header.startswith(b"ID3"):
            return "mp3"
        if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xF6 == 0xF0:
            return "aac"
        if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
            return "mp3"
        if header.startswith(b"OggS"):
            return "ogg"
        if len(header) >= 12 and header[4:8] == b"ftyp":
            return "m4a"
        raise CollectionError("invalid_audio_content")


class StreamingAssetDownloader:
    """Stream large assets to a temporary file, then validate and hand off to storage."""

    def __init__(self, transport: HttpBinaryTransport, validator, directory: Path, max_bytes: int):
        self.transport = transport
        self.validator = validator
        self.directory = directory.resolve()
        self.max_bytes = max_bytes

    async def download(self, asset: MediaAsset):
        from .models import DownloadedFile

        temporary = None
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=self.directory, suffix=".part", delete=False) as stream:
                temporary = Path(stream.name)
            await self.transport.fetch_to_file(asset.url, temporary, self.max_bytes)
            extension = self.validator.validate_file(temporary)
            return DownloadedFile(temporary, extension)
        except CollectionError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except asyncio.CancelledError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        except OSError as exc:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise CollectionError("asset_write_failed") from exc


class ValidatedAssetDownloader:
    """Shared download algorithm; media-specific validation is a strategy."""

    def __init__(self, transport: BinaryTransport, validator: AssetValidator):
        self.transport = transport
        self.validator = validator

    async def download(self, asset: MediaAsset) -> DownloadedAsset:
        return self.validator.validate(await self.transport.fetch(asset.url))
