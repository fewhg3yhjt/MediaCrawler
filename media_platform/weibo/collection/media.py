# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\media.py
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

"""Reusable streaming collector for direct video and audio assets."""

from pathlib import Path
from urllib.parse import urlsplit

from .models import (
    AssetResult, CollectionError, CollectionResult, MediaAsset, MediaManifest, MediaOptions, PostRef, ProgressEvent,
)
from .ports import AssetDownloader, AssetRepository, MediaResolver, ProgressObserver


def _asset(post_id: str, owner_id: str, url: str, kind: str, asset_id: str) -> MediaAsset:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise CollectionError("invalid_media_url")
    identity = asset_id or Path(parsed.path).stem or f"{owner_id}-{kind}"
    return MediaAsset(identity, post_id, owner_id, url, kind)


class WeiboMediaResolver:
    """Extract only direct media URLs; a page link is never treated as a file."""

    _video_keys = ("mp4_720p_mp4", "mp4_hd_mp4", "mp4_sd_mp4")

    def __init__(self, client, gate, kind: str):
        if kind not in ("video", "audio"):
            raise ValueError("media kind must be video or audio")
        self.client = client
        self.gate = gate
        self.kind = kind

    async def resolve(self, post: PostRef, options: MediaOptions) -> MediaManifest:
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

        assets = []
        original_complete = self._extract_owner(post.note_id, raw, assets)
        retweeted = raw.get("retweeted_status")
        if options.include_retweeted and isinstance(retweeted, dict):
            retweeted_complete = self._extract_owner(post.note_id, retweeted, assets)
        else:
            retweeted_complete = retweeted is None
        return MediaManifest(tuple(assets), original_complete, retweeted_complete)

    def _extract_owner(self, post_id: str, owner: dict, assets: list[MediaAsset]) -> bool:
        owner_id = str(owner.get("id") or "")
        if not owner_id:
            return False
        page = owner.get("page_info")
        if page is None:
            return True
        if not isinstance(page, dict):
            raise CollectionError("invalid_media_response")
        urls = page.get("urls") or {}
        if not isinstance(urls, dict):
            raise CollectionError("invalid_media_response")
        page_id = str(page.get("page_id") or page.get("object_id") or owner_id)
        if self.kind == "video":
            url = next((urls.get(key) for key in self._video_keys if isinstance(urls.get(key), str) and urls[key]), None)
            indicated = page.get("type") in ("video", "live") or bool(page.get("page_url")) or any(
                key.startswith("mp4_") for key in urls
            )
            if url:
                assets.append(_asset(post_id, owner_id, url, self.kind, page_id))
                return True
            return not indicated

        url = page.get("audio_url")
        if url:
            if not isinstance(url, str):
                raise CollectionError("invalid_media_response")
            assets.append(_asset(post_id, owner_id, url, self.kind, page_id))
            return True
        return page.get("type") != "audio"


class MediaCollector:
    """Compose a kind-specific resolver, validator/downloader, and shared asset store."""

    def __init__(self, kind: str, resolver: MediaResolver, downloader: AssetDownloader,
                 repository: AssetRepository, scope: str, options_type, observer: ProgressObserver | None = None):
        if kind not in ("video", "audio"):
            raise ValueError("media kind must be video or audio")
        self.name = kind
        self.kind = kind
        self.resolver = resolver
        self.downloader = downloader
        self.repository = repository
        self.scope = scope
        self.options_type = options_type
        self.observer = observer

    async def collect(self, post: PostRef, options: MediaOptions, policy: str) -> CollectionResult:
        try:
            manifest = await self.resolver.resolve(post, options)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)
        selected = [asset for asset in manifest.assets
                    if options.include_retweeted or asset.owner_post_id == post.note_id]
        if self.observer:
            self.observer(ProgressEvent("media", post_id=post.note_id, capability=self.kind, total=len(selected)))
        results: list[AssetResult] = []
        for asset in selected:
            try:
                path = None if not options.download or policy == "refresh" else self.repository.find(self.scope, asset)
                if options.download and path is None:
                    path = self.repository.save(self.scope, asset, await self.downloader.download(asset))
                results.append(AssetResult(asset, "succeeded", str(path) if path else None))
            except CollectionError as exc:
                results.append(AssetResult(asset, "failed", error_code=exc.code))
            if self.observer:
                self.observer(ProgressEvent("media", post_id=post.note_id, capability=self.kind,
                                            completed=len(results), total=len(selected)))
        normalized = tuple(results)
        failures = sum(item.status == "failed" for item in normalized)
        complete = manifest.satisfies(options)
        if not complete:
            status, code = "partial", "media_manifest_unverified"
        elif not normalized:
            status, code = "no_content", None
        elif failures:
            status = "failed" if failures == len(normalized) else "partial"
            code = None
        else:
            status, code = "succeeded", None
        return CollectionResult(status, normalized, error_code=code)
