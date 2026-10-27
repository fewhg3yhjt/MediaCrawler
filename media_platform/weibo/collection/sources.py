# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\sources.py
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

"""Sources discover identities; they never invoke collectors."""

import json
from pathlib import Path
from typing import AsyncIterator

from .models import CollectionError, MediaAsset, ImageManifest, PostRef


class DetailSource:
    def __init__(self, note_ids: tuple[str, ...]):
        self.note_ids = note_ids

    async def discover(self) -> AsyncIterator[PostRef]:
        for note_id in self.note_ids:
            yield PostRef(note_id)


def _urls(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value) if value else []
    if not isinstance(value, list) or any(not isinstance(url, str) for url in value):
        raise CollectionError("invalid_archive_images")
    return value


def image_asset(post_id: str, owner_id: str, url: str, asset_id: str = "") -> MediaAsset:
    from hashlib import sha256
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    if parsed.scheme not in ("https", "http") or not parsed.hostname:
        raise CollectionError("invalid_image_url")
    # CDN size variants and expiring query strings are not asset identities.
    identity = asset_id or Path(parsed.path).stem
    if not identity:
        identity = sha256(url.encode()).hexdigest()
    return MediaAsset(identity, post_id, owner_id, url)


class ArchiveSource:
    def __init__(self, directory: Path, collection: str, note_ids: tuple[str, ...] | None):
        if not collection or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-" for c in collection):
            raise ValueError("invalid collection")
        self.directory = directory
        self.collection = collection
        self.note_ids = None if note_ids is None else set(note_ids)

    async def discover(self) -> AsyncIterator[PostRef]:
        records = {}
        paths = sorted(self.directory.glob(f"{self.collection}_contents_*.jsonl"))
        if not paths:
            raise CollectionError("archive_collection_not_found")
        for path in paths:
            with path.open(encoding="utf-8") as stream:
                for line in stream:
                    if not line.strip():
                        continue
                    try:
                        record = json.loads(line)
                        note_id = str(record["note_id"])
                        if not note_id.isascii() or not note_id.isdigit():
                            raise ValueError("invalid ID")
                    except (ValueError, KeyError, TypeError) as exc:
                        raise CollectionError("invalid_archive_record") from exc
                    if self.note_ids is None or note_id in self.note_ids:
                        records[note_id] = record
        if self.note_ids is not None and not self.note_ids.issubset(records):
            raise CollectionError("archive_note_not_found")
        for note_id, record in records.items():
            assets = [image_asset(note_id, note_id, url) for url in _urls(record.get("image_urls"))]
            rt_id = str(record.get("retweeted_note_id") or "")
            rt_urls = _urls(record.get("rt_images"))
            if rt_urls and not rt_id:
                raise CollectionError("archive_retweet_id_missing")
            assets.extend(image_asset(note_id, rt_id, url) for url in rt_urls)
            # Legacy nonempty values are useful, but cannot prove completeness.
            # No network is needed to reuse them; the result stays partial.
            yield PostRef(note_id, ImageManifest(tuple(assets), False, False))
