# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\session.py
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

"""Explicit HTTP/browser session adapters; never discover credentials implicitly."""

from contextlib import asynccontextmanager, suppress
from pathlib import Path
import os
import json
import re
from urllib.parse import urlsplit

import httpx

from .models import CollectionError


class ProfileLease:
    """OS-released cross-process lease, independent of Chromium's own lock."""

    def __init__(self, profile: Path):
        self.path = profile / ".mediacrawler-session.lock"
        self.stream = None

    def acquire(self):
        stream = self.path.open("a+b")
        try:
            if stream.seek(0, 2) == 0:
                stream.write(b"0")
                stream.flush()
            stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            stream.close()
            raise CollectionError("browser_profile_in_use") from exc
        self.stream = stream

    def release(self):
        if self.stream is None:
            return
        try:
            self.stream.seek(0)
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.stream.fileno(), fcntl.LOCK_UN)
        finally:
            self.stream.close()
            self.stream = None


def decode_detail(status: int, url: str, payload: dict | None, note_id: str) -> dict:
    host = urlsplit(url).hostname or ""
    if status == 401 or host not in ("weibo.com", "www.weibo.com"):
        raise CollectionError("login_required")
    if status == 403:
        raise CollectionError("access_restricted")
    if status != 200:
        raise CollectionError("detail_request_failed")
    if not isinstance(payload, dict) or str(payload.get("id")) != note_id:
        raise CollectionError("detail_unavailable")
    return {"mblog": payload}


def decode_mobile_detail(status: int, url: str, body: str, note_id: str) -> dict:
    host = urlsplit(url).hostname or ""
    if status in (401, 403):
        raise CollectionError("access_restricted" if status == 403 else "login_required")
    if 300 <= status < 400 or host not in ("m.weibo.cn", "www.weibo.cn"):
        raise CollectionError("login_required")
    if status != 200:
        raise CollectionError("full_text_unavailable")
    match = re.search(r"var \$render_data = (\[.*?\])\[0\]", body, re.DOTALL)
    if match is None:
        raise CollectionError("full_text_unavailable")
    try:
        payload = json.loads(match.group(1))
        mblog = payload[0].get("status") if isinstance(payload, list) and payload else None
    except (ValueError, TypeError, AttributeError, IndexError) as exc:
        raise CollectionError("full_text_unavailable") from exc
    if not isinstance(mblog, dict) or str(mblog.get("id")) != note_id:
        raise CollectionError("detail_unavailable")
    return {"mblog": mblog}


def decode_creator_page(status: int, url: str, payload: dict | None) -> dict:
    host = urlsplit(url).hostname or ""
    if status == 401 or host not in ("weibo.com", "www.weibo.com"):
        raise CollectionError("login_required")
    if status == 403:
        raise CollectionError("access_restricted")
    if status != 200:
        raise CollectionError("creator_request_failed")
    if not isinstance(payload, dict):
        raise CollectionError("login_required")
    if payload.get("ok") != 1:
        message = str(payload.get("msg") or payload.get("message") or "").lower()
        if any(token in message for token in ("login", "登录", "passport", "cookie")):
            raise CollectionError("login_required")
        raise CollectionError("access_restricted")
    data = payload.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("list"), list):
        raise CollectionError("invalid_creator_page")
    result = {"list": data["list"]}
    total = data.get("total")
    if isinstance(total, int) and total >= 0:
        result["total"] = total
    if isinstance(data.get("has_more"), bool):
        result["has_more"] = data["has_more"]
    elif not result["list"] and "total" in result:
        result["has_more"] = False
    return result


class HttpWeiboDetailClient:
    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    async def get_note_info_by_id(self, note_id: str) -> dict:
        try:
            response = await self.client.get(
                "https://weibo.com/ajax/statuses/show", params={"id": note_id},
                headers={"Referer": f"https://weibo.com/status/{note_id}"}, follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            raise CollectionError("detail_request_failed") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return decode_detail(response.status_code, str(response.url), payload, note_id)

    async def get_full_text_by_id(self, note_id: str) -> dict:
        try:
            response = await self.client.get(
                f"https://m.weibo.cn/detail/{note_id}",
                headers={"Referer": f"https://m.weibo.cn/detail/{note_id}"},
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise CollectionError("full_text_unavailable") from exc
        return decode_mobile_detail(response.status_code, str(response.url), response.text, note_id)

    async def get_creator_notes_page(self, creator_id: str, page: int) -> dict:
        url = "https://weibo.com/ajax/statuses/mymblog"
        try:
            response = await self.client.get(
                url, params={"uid": creator_id, "page": page, "feature": 0},
                headers={"Referer": f"https://weibo.com/u/{creator_id}"}, follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise CollectionError("creator_request_failed") from exc
        try:
            payload = response.json()
        except ValueError:
            payload = None
        return decode_creator_page(response.status_code, str(response.url), payload)


class BrowserProfileSession:
    """Use only a caller-selected profile, after explicit user authorization."""

    def __init__(self, profile: Path, create: bool = False):
        self.profile = profile.resolve()
        self.create = create

    @asynccontextmanager
    async def context(self):
        if self.create:
            self.profile.mkdir(parents=True, exist_ok=True)
        if not self.profile.is_dir():
            raise ValueError("browser profile directory does not exist")
        from playwright.async_api import async_playwright
        lease = ProfileLease(self.profile)
        lease.acquire()
        try:
            async with async_playwright() as playwright:
                try:
                    context = await playwright.chromium.launch_persistent_context(
                        str(self.profile), channel="chrome", headless=True,
                    )
                except Exception as exc:
                    raise CollectionError("browser_profile_unavailable") from exc
                try:
                    yield context
                finally:
                    with suppress(Exception):
                        await context.close()
        finally:
            lease.release()

    @asynccontextmanager
    async def open(self):
        async with self.context() as context:
            yield BrowserWeiboDetailClient(context.request)


class BrowserWeiboDetailClient:
    def __init__(self, request):
        self.request = request

    async def get_note_info_by_id(self, note_id: str) -> dict:
        try:
            response = await self.request.get(
                "https://weibo.com/ajax/statuses/show", params={"id": note_id},
                headers={"Referer": f"https://weibo.com/status/{note_id}"}, timeout=30000,
            )
        except Exception as exc:
            raise CollectionError("detail_request_failed") from exc
        try:
            payload = await response.json()
        except ValueError:
            payload = None
        return decode_detail(response.status, response.url, payload, note_id)

    async def get_full_text_by_id(self, note_id: str) -> dict:
        try:
            response = await self.request.get(
                f"https://m.weibo.cn/detail/{note_id}",
                headers={"Referer": f"https://m.weibo.cn/detail/{note_id}"},
                timeout=30000, max_redirects=0,
            )
            body = await response.text()
        except Exception as exc:
            raise CollectionError("full_text_unavailable") from exc
        return decode_mobile_detail(response.status, response.url, body, note_id)

    async def get_creator_notes_page(self, creator_id: str, page: int) -> dict:
        try:
            response = await self.request.get(
                "https://weibo.com/ajax/statuses/mymblog",
                params={"uid": creator_id, "page": page, "feature": 0},
                headers={"Referer": f"https://weibo.com/u/{creator_id}"},
                timeout=30000, max_redirects=0,
            )
        except Exception as exc:
            raise CollectionError("creator_request_failed") from exc
        try:
            payload = await response.json()
        except ValueError:
            payload = None
        return decode_creator_page(response.status, response.url, payload)
