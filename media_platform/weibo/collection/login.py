# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\login.py
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

"""Explicit QR login for a selected post; session material stays in Chromium."""

import asyncio
import io
import math
import time
from urllib.parse import urlencode, urlsplit
from contextlib import suppress

from PIL import Image

from .models import CollectionError
from .session import BrowserWeiboDetailClient


class QrLoginFlow:
    def __init__(self, timeout: float = 180, refresh_after: float = 60):
        if not math.isfinite(timeout) or not math.isfinite(refresh_after) or timeout <= 0 or refresh_after <= 0:
            raise ValueError("login timing must be positive")
        self.timeout = timeout
        self.refresh_after = refresh_after

    async def ensure(self, context, note_id: str, on_qr):
        client = BrowserWeiboDetailClient(context.request)
        try:
            await client.get_note_info_by_id(note_id)
            return
        except CollectionError as exc:
            if exc.code not in ("login_required", "access_restricted"):
                raise
        page = await context.new_page()
        login_url = "https://passport.weibo.com/sso/signin?" + urlencode({
            "entry": "miniblog", "source": "miniblog",
            "url": f"https://weibo.com/status/{note_id}",
        })
        try:
            await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
            deadline = time.monotonic() + self.timeout
            refreshed = 0.0
            while time.monotonic() < deadline:
                host = urlsplit(page.url).hostname or ""
                if host in ("weibo.com", "www.weibo.com"):
                    await client.get_note_info_by_id(note_id)
                    return
                if time.monotonic() - refreshed >= self.refresh_after:
                    if refreshed:
                        await page.goto(login_url, wait_until="domcontentloaded", timeout=30000)
                    qr = page.locator("img.w-full.h-full").first
                    await qr.wait_for(state="visible", timeout=20000)
                    await qr.evaluate("img => img.decode()")
                    await page.wait_for_timeout(100)
                    content = await qr.screenshot()
                    with Image.open(io.BytesIO(content)) as image:
                        if image.convert("L").getextrema()[0] == image.convert("L").getextrema()[1]:
                            raise CollectionError("qr_unavailable")
                    on_qr(content)
                    refreshed = time.monotonic()
                await asyncio.sleep(1)
            raise CollectionError("login_timeout")
        except CollectionError:
            raise
        except Exception as exc:
            raise CollectionError("qr_unavailable") from exc
        finally:
            with suppress(Exception):
                await page.close()
