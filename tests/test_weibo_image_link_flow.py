# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_image_link_flow.py
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

"""Link -> authenticated detail -> image bytes -> verified files -> gallery."""

import asyncio
import importlib.util
import io
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from PIL import Image

from media_platform.weibo.collection.links import WeiboPostLink
from media_platform.weibo.collection.models import CrawlRequest, CollectionError
from media_platform.weibo.collection.session import HttpWeiboDetailClient


@pytest.mark.parametrize("link", [
    "https://weibo.com/3783157493/RotDsn5pW?from=feed#comment",
    "https://m.weibo.cn/status/RotDsn5pW",
    "https://m.weibo.cn/detail/5358337945502372",
    "https://weibo.com/status/5358337945502372",
    "5358337945502372",
])
def test_link_identity(link):
    assert WeiboPostLink.parse(link).note_id == "5358337945502372"
    request = CrawlRequest.model_validate({"source": {"type": "detail", "note_ids": [link]},
                                          "collect": {"images": {}}})
    assert request.source.note_ids == ("5358337945502372",)


@pytest.mark.parametrize("link", [
    "https://weibo.com.evil.test/3783157493/RotDsn5pW",
    "https://weibo.com@evil.test/3783157493/RotDsn5pW",
    "https://user:password@weibo.com/3783157493/RotDsn5pW",
    "https://weibo.com/u/3783157493", "file:///tmp/image", "0",
    "https://weibo.com/3783157493/../../something",
])
def test_rejects_nonpost_links(link):
    with pytest.raises(ValueError):
        WeiboPostLink.parse(link)


@pytest.mark.parametrize("status,url,code", [
    (200, "https://passport.weibo.com/login", "login_required"),
    (403, "https://weibo.com/ajax/statuses/show", "access_restricted"),
    (200, "https://weibo.com/ajax/statuses/show", "detail_unavailable"),
])
def test_login_and_missing_details_are_explicit(status, url, code):
    async def execute():
        def respond(request):
            if "passport" in url and request.url.host != "passport.weibo.com":
                return httpx.Response(302, headers={"Location": url})
            return httpx.Response(status, text="<html>login</html>")
        transport = httpx.MockTransport(respond)
        async with httpx.AsyncClient(transport=transport) as client:
            await HttpWeiboDetailClient(client).get_note_info_by_id("5358337945502372")
    with pytest.raises(CollectionError, match=code):
        asyncio.run(execute())


def test_full_cli_flow_downloads_files_and_writes_gallery(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("collect_weibo_link_test", root / "scripts/collect_weibo.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    payload = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(payload, format="PNG")
    image_bytes = payload.getvalue()
    requests = []

    def respond(request):
        requests.append((request.url.host, request.headers.get("Cookie")))
        if request.url.host == "weibo.com":
            assert request.url.params["id"] == "5358337945502372"
            return httpx.Response(200, json={
                "id": "5358337945502372", "pic_num": 1,
                "pics": [{"pid": "picture", "large": {"url": "https://wx1.sinaimg.cn/large/picture.jpg"}}],
            })
        assert request.url.host == "wx1.sinaimg.cn"
        return httpx.Response(200, content=image_bytes)

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(respond), **kw))
    cookie_file = tmp_path / "cookie.txt"
    cookie_file.write_text("test-session=example", encoding="utf-8")
    args = SimpleNamespace(request_file=None, url="https://weibo.com/3783157493/RotDsn5pW?from=feed#comment",
                           metadata_only=False, include_retweeted=False, cookie_file=cookie_file,
                           browser_profile=None, interval=0, workspace=tmp_path / "work",
                           account_scope="test", resume_task=None, archive_dir=tmp_path / "archive")
    assert asyncio.run(cli.execute(args)) == 0
    report = json.loads(capsys.readouterr().out)
    asset = report["results"][0]["assets"][0]
    assert asset["status"] == "succeeded"
    assert Path(asset["local_path"]).read_bytes() == image_bytes
    assert Path(asset["local_path"]).suffix == ".png"
    gallery = Path(report["outputs"]["gallery"])
    assert '<img loading="lazy"' in gallery.read_text(encoding="utf-8")
    saved = json.loads(Path(report["outputs"]["manifest"]).read_text(encoding="utf-8"))
    assert saved["status"] == "succeeded"
    assert requests == [("weibo.com", "test-session=example"), ("wx1.sinaimg.cn", None)]
    # A new task rechecks details but reuses the successfully saved file.
    assert asyncio.run(cli.execute(args)) == 0
    capsys.readouterr()
    assert [host for host, _ in requests].count("wx1.sinaimg.cn") == 1
