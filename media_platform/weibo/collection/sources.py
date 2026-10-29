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
from datetime import datetime, timezone

from .metrics import metrics_from_status
from .models import CollectionError, MediaAsset, ImageManifest, PostRef, PostText


class DetailSource:
    def __init__(self, note_ids: tuple[str, ...]):
        self.note_ids = note_ids

    async def discover(self) -> AsyncIterator[PostRef]:
        for note_id in self.note_ids:
            yield PostRef(note_id)


def image_manifest(post_id: str, raw: dict) -> ImageManifest:
    """Normalize image data already present in a detail or timeline response."""
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
    retweeted = raw.get("retweeted_status")
    retweeted_complete = extract_owner(retweeted) if isinstance(retweeted, dict) else retweeted is None
    return ImageManifest(tuple(assets), originals, retweeted_complete)


def post_ref_from_status(raw: dict) -> PostRef:
    if not isinstance(raw, dict):
        raise CollectionError("invalid_post_snapshot")
    note_id = str(raw.get("id") or "")
    if not note_id.isascii() or not note_id.isdigit() or int(note_id) <= 0:
        raise CollectionError("invalid_post_snapshot")
    try:
        observed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        return PostRef(note_id, image_manifest(note_id, raw), _text_snapshot(note_id, raw),
                       metrics_from_status(raw, observed_at))
    except CollectionError:
        raise
    except (ValueError, TypeError, AttributeError) as exc:
        raise CollectionError("invalid_post_snapshot") from exc


def _text_snapshot(note_id: str, raw: dict) -> PostText | None:
    body = raw.get("longTextContent") or raw.get("text")
    value = raw.get("text_raw")
    if body is None and value is None:
        return None
    if body is not None and not isinstance(body, str):
        raise CollectionError("invalid_text_response")
    if value is not None and not isinstance(value, str):
        raise CollectionError("invalid_text_response")
    from .text import visible_text
    if raw.get("longTextContent"):
        value = visible_text(raw["longTextContent"])
    elif not value:
        value = visible_text(body or "")
    complete = not bool(raw.get("isLongText")) or bool(raw.get("longTextContent"))
    return PostText(note_id, value or "", body or value or "", complete)


class CreatorSource:
    """Page through one creator timeline and yield reusable post references."""

    def __init__(self, creator_id: str, client, gate, max_pages: int = 1000):
        self.creator_id = creator_id
        self.client = client
        self.gate = gate
        self.max_pages = max_pages

    async def discover(self) -> AsyncIterator[PostRef]:
        page, empty_pages, discovered = 1, 0, set()
        previous_page_ids = None
        while page <= self.max_pages:
            await self.gate.wait()
            try:
                data = await self.client.get_creator_notes_page(self.creator_id, page)
            except CollectionError:
                raise
            except Exception as exc:
                raise CollectionError("creator_request_failed") from exc
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise CollectionError("invalid_creator_page")

            items = data["list"]
            if not items:
                if data.get("has_more") is False:
                    return
                empty_pages += 1
                if empty_pages >= 2:
                    return
                page += 1
                continue

            empty_pages = 0
            ids = []
            for item in items:
                raw = item.get("mblog") if isinstance(item, dict) and isinstance(item.get("mblog"), dict) else item
                if not isinstance(raw, dict):
                    raise CollectionError("invalid_creator_page")
                try:
                    post = post_ref_from_status(raw)
                except CollectionError as exc:
                    raise CollectionError("invalid_creator_page") from exc
                note_id = post.note_id
                ids.append(note_id)
                if note_id in discovered:
                    continue
                discovered.add(note_id)
                yield post

            signature = tuple(ids)
            if signature == previous_page_ids:
                raise CollectionError("creator_page_repeated")
            previous_page_ids = signature
            total = data.get("total")
            if data.get("has_more") is False:
                return
            if isinstance(total, int) and total > 0 and len(discovered) >= total:
                return
            page += 1
        raise CollectionError("creator_page_limit")



class SelfFeedSource:
    """Discover the current account's favorite or liked timeline."""

    def __init__(self, feed_type: str, client, gate, max_pages: int = 1000):
        if feed_type not in ("favorite", "liked"):
            raise ValueError("self feed must be favorite or liked")
        self.feed_type = feed_type
        self.client = client
        self.gate = gate
        self.max_pages = max_pages

    async def discover(self) -> AsyncIterator[PostRef]:
        await self.gate.wait()
        try:
            uid = await self.client.get_current_user_uid()
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("current_user_unavailable") from exc
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
            raise CollectionError("current_user_unavailable")

        page, empty_pages, discovered = 1, 0, set()
        previous_page_ids = None
        while page <= self.max_pages:
            await self.gate.wait()
            try:
                data = await self.client.get_self_feed_page(self.feed_type, uid, page)
            except CollectionError:
                raise
            except Exception as exc:
                raise CollectionError("self_feed_request_failed") from exc
            if not isinstance(data, dict) or not isinstance(data.get("list"), list):
                raise CollectionError("invalid_self_feed_page")
            items = data["list"]
            if not items:
                if data.get("has_more") is False:
                    return
                empty_pages += 1
                if empty_pages >= 2:
                    if isinstance(data.get("total"), int) and len(discovered) < data["total"]:
                        raise CollectionError("empty_self_feed_page_unverified")
                    return
                page += 1
                continue

            empty_pages = 0
            ids = []
            for raw in items:
                status = raw.get("mblog") if isinstance(raw, dict) and isinstance(raw.get("mblog"), dict) else raw
                try:
                    post = post_ref_from_status(status)
                except CollectionError as exc:
                    raise CollectionError("invalid_self_feed_page") from exc
                note_id = post.note_id
                ids.append(note_id)
                if note_id not in discovered:
                    discovered.add(note_id)
                    yield post

            signature = tuple(ids)
            if signature == previous_page_ids:
                raise CollectionError("self_feed_page_repeated")
            previous_page_ids = signature
            total = data.get("total")
            if isinstance(total, int) and total > 0 and len(discovered) >= total:
                return
            if data.get("has_more") is False:
                return
            page += 1
        raise CollectionError("self_feed_page_limit")


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
            body = record.get("content")
            text = None
            if isinstance(body, str):
                raw_text = record.get("raw_text")
                if not isinstance(raw_text, str):
                    raw_text = body
                text = PostText(note_id, body, raw_text, record.get("text_complete") is True)
            yield PostRef(note_id, ImageManifest(tuple(assets), False, False), text,
                          metrics_from_status(record))
