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
import hashlib
from pathlib import Path
from typing import AsyncIterator
from datetime import datetime, timezone

from .metrics import metrics_from_status
from .pagination import PageContinuity
from .models import (
    CollectionError, DiscoveryBatch, MediaAsset, ImageManifest, PostRef, PostText,
    SourceCheckpoint,
)


def _source_cursor(checkpoint: SourceCheckpoint | None, source_key: str) -> dict:
    if checkpoint is None:
        return {}
    if checkpoint.source_key != source_key:
        raise CollectionError("source_checkpoint_mismatch")
    if not isinstance(checkpoint.cursor, dict):
        raise CollectionError("source_checkpoint_invalid")
    return checkpoint.cursor


def _cursor_int(cursor: dict, name: str, default: int, minimum: int = 0) -> int:
    value = cursor.get(name, default)
    if type(value) is not int or value < minimum:
        raise CollectionError("source_checkpoint_invalid")
    return value


def _source_key(kind: str, identity) -> str:
    value = json.dumps(identity, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return f"{kind}:{hashlib.sha256(value.encode('utf-8')).hexdigest()}"


class DetailSource:
    def __init__(self, note_ids: tuple[str, ...]):
        self.note_ids = note_ids
        self.checkpoint_key = _source_key("detail", note_ids)

    async def validate_checkpoint(self, checkpoint: SourceCheckpoint | None) -> None:
        _source_cursor(checkpoint, self.checkpoint_key)

    async def discover(self, checkpoint: SourceCheckpoint | None = None) -> AsyncIterator[DiscoveryBatch]:
        if checkpoint is not None and checkpoint.complete:
            return
        cursor = _source_cursor(checkpoint, self.checkpoint_key)
        index = _cursor_int(cursor, "index", 0)
        if index > len(self.note_ids):
            raise CollectionError("source_checkpoint_invalid")
        for offset in range(index, len(self.note_ids)):
            yield DiscoveryBatch(
                (PostRef(self.note_ids[offset]),),
                SourceCheckpoint(self.checkpoint_key, {"index": offset + 1}, offset + 1 == len(self.note_ids)),
            )


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
        self.checkpoint_key = _source_key("creator", creator_id)

    async def validate_checkpoint(self, checkpoint: SourceCheckpoint | None) -> None:
        _source_cursor(checkpoint, self.checkpoint_key)

    async def discover(self, checkpoint: SourceCheckpoint | None = None) -> AsyncIterator[DiscoveryBatch]:
        if checkpoint is not None and checkpoint.complete:
            return
        cursor = _source_cursor(checkpoint, self.checkpoint_key)
        continuity = PageContinuity(cursor)
        page, empty_pages = continuity.start_page, continuity.empty_pages
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
            total = data.get("total")
            known_total = total if type(total) is int and total > 0 else None
            ids = []
            posts = []
            page_seen = set()
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
                if note_id not in page_seen:
                    page_seen.add(note_id)
                    posts.append(post)

            continuity.validate_page_signature(page, ids, "creator_page_repeated")
            if continuity.is_overlap_page(page):
                continuity.observe_overlap(page, ids)
                yield DiscoveryBatch(tuple(posts), checkpoint)
                if page == continuity.replay_end:
                    continuity.finish_overlap()
                    yield DiscoveryBatch((), SourceCheckpoint(
                        self.checkpoint_key,
                        continuity.cursor(continuity.frontier, continuity.empty_pages),
                        False,
                    ))
                page += 1
                continue

            if not items:
                continuity.observe_forward(page, ids)
                if data.get("has_more") is False:
                    complete = True
                    yield DiscoveryBatch((), SourceCheckpoint(
                        self.checkpoint_key,
                        continuity.cursor(page + 1, empty_pages, complete),
                        complete,
                    ), known_total, "has_more_false")
                    return
                empty_pages += 1
                complete = empty_pages >= 2
                yield DiscoveryBatch((), SourceCheckpoint(
                    self.checkpoint_key,
                    continuity.cursor(page + 1, empty_pages, complete),
                    complete,
                ), known_total, "empty_pages" if complete else None)
                if complete:
                    return
                page += 1
                continue

            empty_pages = 0
            continuity.observe_forward(page, ids)
            complete = data.get("has_more") is False
            yield DiscoveryBatch(tuple(posts), SourceCheckpoint(
                self.checkpoint_key,
                continuity.cursor(page + 1, empty_pages, complete),
                complete,
            ), known_total, "has_more_false" if complete else None)
            if complete:
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
        self.checkpoint_key = _source_key("self_feed", feed_type)
        self._validated_uid: str | None = None

    async def validate_checkpoint(self, checkpoint: SourceCheckpoint | None) -> None:
        cursor = _source_cursor(checkpoint, self.checkpoint_key)
        expected_uid = cursor.get("uid")
        if expected_uid is None:
            return
        self._validated_uid = await self._current_uid()
        if expected_uid != self._validated_uid:
            raise CollectionError("source_identity_changed")

    async def _current_uid(self) -> str:
        await self.gate.wait()
        try:
            uid = await self.client.get_current_user_uid()
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("current_user_unavailable") from exc
        if not isinstance(uid, str) or not uid.isascii() or not uid.isdigit() or int(uid) <= 0:
            raise CollectionError("current_user_unavailable")
        return uid

    async def discover(self, checkpoint: SourceCheckpoint | None = None) -> AsyncIterator[DiscoveryBatch]:
        if checkpoint is not None and checkpoint.complete:
            return
        cursor = _source_cursor(checkpoint, self.checkpoint_key)
        uid = self._validated_uid or await self._current_uid()
        expected_uid = cursor.get("uid")
        if expected_uid is not None and expected_uid != uid:
            raise CollectionError("source_identity_changed")
        continuity = PageContinuity(cursor)
        continuity.uid = uid
        page, empty_pages = continuity.start_page, continuity.empty_pages
        if expected_uid is None:
            # Bind the task to the authenticated account before requesting or persisting any page.
            # If the process stops mid-page, the next attempt must verify the same account.
            yield DiscoveryBatch((), SourceCheckpoint(
                self.checkpoint_key,
                continuity.cursor(continuity.frontier, empty_pages),
                False,
            ))
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
            total = data.get("total")
            known_total = total if self.feed_type == "favorite" and type(total) is int and total > 0 else None
            ids = []
            posts = []
            page_seen = set()
            for raw in items:
                status = raw.get("mblog") if isinstance(raw, dict) and isinstance(raw.get("mblog"), dict) else raw
                try:
                    post = post_ref_from_status(status)
                except CollectionError as exc:
                    raise CollectionError("invalid_self_feed_page") from exc
                note_id = post.note_id
                ids.append(note_id)
                if note_id not in page_seen:
                    page_seen.add(note_id)
                    posts.append(post)

            continuity.validate_page_signature(page, ids, "self_feed_page_repeated")
            if continuity.is_overlap_page(page):
                continuity.observe_overlap(page, ids)
                yield DiscoveryBatch(tuple(posts), checkpoint)
                if page == continuity.replay_end:
                    continuity.finish_overlap()
                    yield DiscoveryBatch((), SourceCheckpoint(
                        self.checkpoint_key,
                        continuity.cursor(continuity.frontier, continuity.empty_pages),
                        False,
                    ))
                page += 1
                continue

            if not items:
                continuity.observe_forward(page, ids)
                if data.get("has_more") is False:
                    yield DiscoveryBatch((), SourceCheckpoint(
                        self.checkpoint_key,
                        continuity.cursor(page + 1, empty_pages, True),
                        True,
                    ), known_total, "has_more_false", self.feed_type == "favorite")
                    return
                empty_pages += 1
                complete = empty_pages >= 2
                yield DiscoveryBatch((), SourceCheckpoint(
                    self.checkpoint_key,
                    continuity.cursor(page + 1, empty_pages, complete),
                    complete,
                ), known_total, "empty_pages" if complete else None, self.feed_type == "favorite")
                if complete:
                    return
                page += 1
                continue

            empty_pages = 0
            continuity.observe_forward(page, ids)
            complete = data.get("has_more") is False
            yield DiscoveryBatch(tuple(posts), SourceCheckpoint(
                self.checkpoint_key,
                continuity.cursor(page + 1, empty_pages, complete),
                complete,
            ), known_total, "has_more_false" if complete else None, self.feed_type == "favorite")
            if complete:
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
        self.checkpoint_key = _source_key("archive", (str(directory.resolve()), collection, sorted(self.note_ids) if self.note_ids else None))

    async def validate_checkpoint(self, checkpoint: SourceCheckpoint | None) -> None:
        _source_cursor(checkpoint, self.checkpoint_key)

    async def discover(self, checkpoint: SourceCheckpoint | None = None) -> AsyncIterator[DiscoveryBatch]:
        if checkpoint is not None and checkpoint.complete:
            return
        cursor = _source_cursor(checkpoint, self.checkpoint_key)
        index = _cursor_int(cursor, "index", 0)
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
        note_ids = sorted(records)
        signature = hashlib.sha256("\n".join(note_ids).encode("utf-8")).hexdigest()
        if cursor.get("signature", signature) != signature:
            raise CollectionError("source_changed_since_checkpoint")
        if index > len(note_ids):
            raise CollectionError("source_checkpoint_invalid")
        for offset in range(index, len(note_ids)):
            note_id = note_ids[offset]
            record = records[note_id]
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
            yield DiscoveryBatch((PostRef(note_id, ImageManifest(tuple(assets), False, False), text,
                                           metrics_from_status(record)),),
                                 SourceCheckpoint(self.checkpoint_key,
                                                  {"index": offset + 1, "signature": signature},
                                                  offset + 1 == len(note_ids)))
        if not note_ids and index == 0:
            yield DiscoveryBatch((), SourceCheckpoint(self.checkpoint_key, {"index": 0, "signature": signature}, True))
