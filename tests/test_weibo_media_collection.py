# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_media_collection.py
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

"""Offline tests for video/audio extraction, streaming, validation, and reuse."""

import asyncio
from pathlib import Path

import httpx
import pytest

from media_platform.weibo.collection.adapters import (
    AudioFileValidator, HttpBinaryTransport, RequestGate, StreamingAssetDownloader, VideoFileValidator,
)
from media_platform.weibo.collection.media import MediaCollector, WeiboMediaResolver
from media_platform.weibo.collection.models import (
    CollectionError, DownloadedFile, MediaAsset, MediaManifest, PostRef, VideoOptions, AudioOptions,
)
from media_platform.weibo.collection.repository import FileAssetRepository


def run(coro):
    return asyncio.run(coro)


class DetailClient:
    def __init__(self, raw):
        self.raw = raw
        self.calls = []

    async def get_note_info_by_id(self, note_id):
        self.calls.append(note_id)
        return {"mblog": self.raw}


class FileTransport:
    def __init__(self, content):
        self.content = content
        self.calls = []

    async def fetch_to_file(self, url, path, max_bytes):
        self.calls.append((url, max_bytes))
        if len(self.content) > max_bytes:
            raise CollectionError("asset_too_large")
        path.write_bytes(self.content)
        return len(self.content)


VIDEO = b"\x00\x00\x00\x18ftypmp42video-content"
AUDIO = b"ID3audio-content"


def test_video_resolver_selects_best_direct_variant_and_preserves_retweet_owner():
    client = DetailClient({
        "id": "123", "page_info": {"type": "video", "page_id": "main",
            "page_url": "https://weibo.com/tv/show/1", "urls": {
                "mp4_720p_mp4": "https://f.video.weibocdn.com/main.mp4?sig=one",
                "mp4_sd_mp4": "https://f.video.weibocdn.com/main-sd.mp4",
            }},
        "retweeted_status": {"id": "456", "page_info": {"type": "video", "page_id": "retweet",
            "urls": {"mp4_hd_mp4": "https://f.video.weibocdn.com/retweet.mp4"}}},
    })
    resolver = WeiboMediaResolver(client, RequestGate(0), "video")
    own = run(resolver.resolve(PostRef("123"), VideoOptions()))
    both = run(resolver.resolve(PostRef("123"), VideoOptions(include_retweeted=True)))
    assert len(own.assets) == 1 and own.assets[0].url.endswith("sig=one")
    assert [a.owner_post_id for a in both.assets] == ["123", "456"]
    assert client.calls == ["123", "123"]


def test_direct_page_link_is_not_claimed_as_a_downloadable_video():
    client = DetailClient({"id": "123", "page_info": {"type": "video", "page_url": "https://weibo.com/tv/1"}})
    manifest = run(WeiboMediaResolver(client, RequestGate(0), "video").resolve(
        PostRef("123"), VideoOptions(download=False)))
    assert manifest.assets == () and not manifest.originals_complete


def test_audio_resolver_uses_audio_url_and_downloads_without_memory_buffering(tmp_path):
    client = DetailClient({"id": "123", "page_info": {
        "type": "audio", "page_id": "track", "audio_url": "https://audio.weibocdn.com/track.mp3?token=x",
    }})
    resolver = WeiboMediaResolver(client, RequestGate(0), "audio")
    transport = FileTransport(AUDIO)
    repository = FileAssetRepository(tmp_path / "audio")
    downloader = StreamingAssetDownloader(transport, AudioFileValidator(), repository.root, 1024)
    collector = MediaCollector("audio", resolver, downloader, repository, "scope", AudioOptions)
    result = run(collector.collect(PostRef("123"), AudioOptions(), "missing"))
    assert result.status == "succeeded" and Path(result.assets[0].local_path).read_bytes() == AUDIO
    assert transport.calls == [("https://audio.weibocdn.com/track.mp3?token=x", 1024)]
    assert list(repository.root.glob("*.part")) == []


def test_video_asset_reuses_stored_file_and_refresh_redownloads(tmp_path):
    asset = MediaAsset("page", "123", "123", "https://f.video.weibocdn.com/a.mp4", "video")
    manifest = MediaManifest((asset,), True, True)

    class Resolver:
        async def resolve(self, post, options):
            return manifest

    repository = FileAssetRepository(tmp_path / "video")
    transport = FileTransport(VIDEO)
    downloader = StreamingAssetDownloader(transport, VideoFileValidator(), repository.root, 1024)
    collector = MediaCollector("video", Resolver(), downloader, repository, "scope", VideoOptions)
    first = run(collector.collect(PostRef("123"), VideoOptions(), "missing"))
    second = run(collector.collect(PostRef("123"), VideoOptions(), "missing"))
    refreshed = run(collector.collect(PostRef("123"), VideoOptions(), "refresh"))
    assert first.status == second.status == refreshed.status == "succeeded"
    assert len(transport.calls) == 2
    assert repository.find("scope", asset) == Path(refreshed.assets[0].local_path)


@pytest.mark.parametrize("kind,content,expected", [
    ("video", VIDEO, "mp4"), ("video", b"\x1a\x45\xdf\xa3webm", "webm"),
    ("audio", AUDIO, "mp3"), ("audio", b"OggSsound", "ogg"),
    ("audio", b"\xff\xf1\x50\x80aac", "aac"),
])
def test_container_validators_detect_content_not_url_extension(tmp_path, kind, content, expected):
    path = tmp_path / "download.part"
    path.write_bytes(content)
    validator = VideoFileValidator() if kind == "video" else AudioFileValidator()
    assert validator.validate_file(path) == expected


def test_container_validators_reject_html_and_empty_data(tmp_path):
    path = tmp_path / "download.part"
    for content, validator, code in [
        (b"<html>not a video</html>", VideoFileValidator(), "invalid_video_content"),
        (b"<html>not audio</html>", AudioFileValidator(), "invalid_audio_content"),
    ]:
        path.write_bytes(content)
        with pytest.raises(CollectionError, match=code):
            validator.validate_file(path)


def test_stream_transport_follows_only_trusted_cdn_redirects_and_enforces_size(tmp_path):
    calls = []

    def handler(request):
        calls.append(request.url.host)
        if request.url.path == "/start":
            return httpx.Response(302, headers={"Location": "https://evil.example/file.mp4"})
        return httpx.Response(200, content=VIDEO)

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await HttpBinaryTransport(client, RequestGate(0)).fetch_to_file(
                "https://f.video.weibocdn.com/start", tmp_path / "media.part", 1000,
            )

    with pytest.raises(CollectionError, match="unsupported_media_host"):
        run(execute())
    assert calls == ["f.video.weibocdn.com"]
    assert not (tmp_path / "media.part").exists()


def test_stream_transport_caps_response_bytes_and_cleans_partial_file(tmp_path):
    async def execute():
        handler = httpx.MockTransport(lambda request: httpx.Response(200, content=VIDEO))
        async with httpx.AsyncClient(transport=handler) as client:
            return await HttpBinaryTransport(client, RequestGate(0)).fetch_to_file(
                "https://f.video.weibocdn.com/a.mp4", tmp_path / "partial.part", 5,
            )

    with pytest.raises(CollectionError, match="asset_too_large"):
        run(execute())
    assert not (tmp_path / "partial.part").exists()


def test_file_repository_rejects_temp_files_outside_its_workspace(tmp_path):
    repository = FileAssetRepository(tmp_path / "assets")
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(VIDEO)
    asset = MediaAsset("a", "123", "123", "https://f.video.weibocdn.com/a.mp4", "video")
    with pytest.raises(CollectionError, match="invalid_asset_data"):
        repository.save("scope", asset, DownloadedFile(outside, "mp4"))


def test_cancelling_stream_removes_partial_file(tmp_path):
    class CancelingTransport:
        async def fetch_to_file(self, url, path, max_bytes):
            path.write_bytes(VIDEO[:8])
            raise asyncio.CancelledError()

    downloader = StreamingAssetDownloader(CancelingTransport(), VideoFileValidator(), tmp_path, 1024)
    with pytest.raises(asyncio.CancelledError):
        run(downloader.download(MediaAsset("a", "123", "123", "https://f.video.weibocdn.com/a.mp4", "video")))
    assert list(tmp_path.iterdir()) == []
