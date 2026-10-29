# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_image_boundaries.py
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

"""Image-specific boundaries: animated files, interruption and session leases."""

import asyncio
import io

import pytest
from PIL import Image

from media_platform.weibo.collection.adapters import ImageValidator, RequestGate
from media_platform.weibo.collection.images import ImageCollector
from media_platform.weibo.collection.models import (
    CollectionError, CrawlRequest, DownloadedAsset, ImageManifest, ImageOptions, MediaAsset, PostRef,
)
from media_platform.weibo.collection.repository import FileAssetRepository, SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner
from media_platform.weibo.collection.session import ProfileLease
from media_platform.weibo.collection.sources import DetailSource


def image_bytes():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()


def test_animated_gif_is_preserved_without_transcoding():
    stream = io.BytesIO()
    first, second = Image.new("RGB", (3, 2), "red"), Image.new("RGB", (3, 2), "blue")
    first.save(stream, format="GIF", save_all=True, append_images=[second], duration=100, loop=0)
    content = stream.getvalue()
    downloaded = ImageValidator().validate(content)
    assert downloaded.extension == "gif" and downloaded.content == content
    with Image.open(io.BytesIO(downloaded.content)) as image:
        assert image.n_frames == 2


def test_truncated_jpeg_is_not_download_success():
    stream = io.BytesIO()
    Image.new("RGB", (300, 300), "red").save(stream, format="JPEG")
    with pytest.raises(CollectionError, match="invalid_image_content"):
        ImageValidator().validate(stream.getvalue()[:-40])


def test_cancel_after_first_image_recovers_only_missing_image(tmp_path):
    class Resolver:
        async def resolve(self, post, options):
            return ImageManifest(tuple(MediaAsset(name, "123", "123", f"https://example.org/{name}.png")
                                       for name in ("a", "b")), True, True)

    class Downloader:
        def __init__(self):
            self.calls = []
            self.cancel = True

        async def download(self, asset):
            self.calls.append(asset.asset_id)
            if asset.asset_id == "b" and self.cancel:
                raise asyncio.CancelledError()
            return DownloadedAsset(image_bytes(), "png")

    events = []
    downloader = Downloader()
    collector = ImageCollector(Resolver(), downloader, FileAssetRepository(tmp_path / "images"), "test", events.append)
    request = CrawlRequest.model_validate({"source": {"type": "detail", "note_ids": ["123"]},
                                          "collect": {"images": {"download": True}}})
    plan = CollectionPlanner((collector,)).plan(request)
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    runner = TaskRunner(repository, "test", events.append)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(runner.run(plan, DetailSource(("123",))))
    task_id = next(e.task_id for e in events if e.phase == "task_started")
    assert repository.report(task_id)["status"] == "cancelled"
    downloader.cancel = False
    assert asyncio.run(runner.run(plan, DetailSource(("123",)), task_id))["status"] == "succeeded"
    assert downloader.calls == ["a", "b", "b"]
    assert [e.completed for e in events if e.phase == "images"] == [0, 1, 0, 1, 2]


def test_retry_resolves_an_expired_link_again(tmp_path):
    class Resolver:
        def __init__(self):
            self.calls = 0

        async def resolve(self, post, options):
            self.calls += 1
            suffix = "expired" if self.calls == 1 else "fresh"
            return ImageManifest((MediaAsset("a", "123", "123", f"https://example.org/{suffix}"),), True, True)

    class Downloader:
        async def download(self, asset):
            if asset.url.endswith("expired"):
                raise CollectionError("asset_request_failed")
            return DownloadedAsset(image_bytes(), "png")

    resolver = Resolver()
    collector = ImageCollector(resolver, Downloader(), FileAssetRepository(tmp_path / "images"), "test")
    assert asyncio.run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing")).status == "failed"
    result = asyncio.run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    assert result.status == "succeeded" and resolver.calls == 2


def test_profile_lease_excludes_second_session_and_releases(tmp_path):
    first, second = ProfileLease(tmp_path), ProfileLease(tmp_path)
    first.acquire()
    try:
        with pytest.raises(CollectionError, match="browser_profile_in_use"):
            second.acquire()
    finally:
        first.release()
    second.acquire()
    second.release()


@pytest.mark.parametrize("value", [-1, float("nan"), float("inf")])
def test_invalid_request_interval(value):
    with pytest.raises(ValueError):
        RequestGate(value)
