# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_text_collection.py
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

"""Offline behavior tests for the independent Weibo text capability."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError
import httpx

from media_platform.weibo.collection.adapters import CachedDetailClient, RequestGate, WeiboImageResolver
from media_platform.weibo.collection.images import ImageCollector
from media_platform.weibo.collection.models import (
    CollectionError, CrawlRequest, ImageOptions, PostRef, PostText, TextOptions,
)
from media_platform.weibo.collection.repository import FileAssetRepository, SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner
from media_platform.weibo.collection.session import HttpWeiboDetailClient, decode_mobile_detail
from media_platform.weibo.collection.sources import ArchiveSource, DetailSource
from media_platform.weibo.collection.text import TextCollector, WeiboTextResolver, visible_text


def run(coro):
    return asyncio.run(coro)


class DetailClient:
    def __init__(self, raw=None, error=None, full_raw=None):
        self.raw = raw or {"id": "123", "text": "Hello<br>world", "pics": []}
        self.error = error
        self.full_raw = full_raw
        self.calls = []
        self.full_calls = []

    async def get_note_info_by_id(self, note_id):
        self.calls.append(note_id)
        if self.error:
            raise self.error
        return {"mblog": self.raw}

    async def get_full_text_by_id(self, note_id):
        self.full_calls.append(note_id)
        if self.full_raw is None:
            raise CollectionError("full_text_unavailable")
        return {"mblog": self.full_raw}


class PassiveDownloader:
    async def download(self, asset):
        raise AssertionError("the fixture has no images to download")


def test_html_to_text_keeps_breaks_entities_and_discards_scripts():
    assert visible_text("<p>A &amp; B</p><p>C<br>D</p><script>secret()</script>") == "A & B\nC\nD"


def test_short_text_is_cleaned_and_marked_complete():
    client = DetailClient({"id": "123", "text": "<p>A &amp; B</p><p>C<br>D</p>", "pics": []})
    collector = TextCollector(WeiboTextResolver(client, RequestGate(0)))
    result = run(collector.collect(PostRef("123"), TextOptions(), "missing"))
    assert result.status == "succeeded"
    assert result.content.text == "A & B\nC\nD" and result.content.complete
    assert client.calls == ["123"]


def test_long_text_snippet_is_partial_when_full_text_is_required():
    client = DetailClient({"id": "123", "text": "excerpt", "isLongText": True, "pics": []})
    collector = TextCollector(WeiboTextResolver(client, RequestGate(0)))
    result = run(collector.collect(PostRef("123"), TextOptions(), "missing"))
    assert result.status == "partial" and result.error_code == "full_text_unavailable"
    assert result.content.text == "excerpt" and result.content.complete is False


def test_embedded_full_text_completes_a_long_post():
    client = DetailClient({"id": "123", "text": "excerpt", "isLongText": True,
                           "longTextContent": "<p>Full<br>body</p>", "pics": []})
    result = run(TextCollector(WeiboTextResolver(client, RequestGate(0))).collect(
        PostRef("123"), TextOptions(), "missing"))
    assert result.status == "succeeded"
    assert result.content.text == "Full\nbody" and result.content.complete


def test_long_text_source_fetches_and_marks_the_full_body():
    client = DetailClient(
        {"id": "123", "text": "short excerpt", "isLongText": True, "pics": []},
        full_raw={"id": "123", "text": "<p>First part</p><p>Full second part</p>", "isLongText": True},
    )
    result = run(TextCollector(WeiboTextResolver(client, RequestGate(0), client)).collect(
        PostRef("123"), TextOptions(), "missing"))
    assert result.status == "succeeded" and result.content.complete
    assert result.content.text == "First part\nFull second part"
    assert client.calls == ["123"] and client.full_calls == ["123"]


def test_long_text_failure_keeps_excerpt_but_marks_partial():
    client = DetailClient({"id": "123", "text": "excerpt", "isLongText": True, "pics": []})
    result = run(TextCollector(WeiboTextResolver(client, RequestGate(0), client)).collect(
        PostRef("123"), TextOptions(), "missing"))
    assert result.status == "partial" and result.content.text == "excerpt"
    assert result.error_code == "full_text_unavailable"


def test_mobile_detail_adapter_extracts_full_rendered_status_and_rejects_redirects():
    html = 'var $render_data = [{"status":{"id":"123","text":"full body"}}][0]'
    assert decode_mobile_detail(200, "https://m.weibo.cn/detail/123", html, "123")["mblog"]["text"] == "full body"
    with pytest.raises(CollectionError, match="login_required"):
        decode_mobile_detail(302, "https://m.weibo.cn/detail/123", "", "123")
    with pytest.raises(CollectionError, match="login_required"):
        decode_mobile_detail(200, "https://passport.weibo.com/login", html, "123")


def test_http_long_text_adapter_does_not_follow_login_redirect():
    requests = []

    def handler(request):
        requests.append(request.url.host)
        return httpx.Response(302, headers={"Location": "https://passport.weibo.com/login"})

    async def execute():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            return await HttpWeiboDetailClient(http).get_full_text_by_id("123")

    with pytest.raises(CollectionError, match="login_required"):
        run(execute())
    assert requests == ["m.weibo.cn"]


def test_incomplete_archive_text_is_explicitly_reusable_only_when_requested(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "favorite_contents_sample.jsonl").write_text(json.dumps({
        "note_id": "123", "content": "legacy body", "raw_text": "<p>legacy body</p>"
    }) + "\n", encoding="utf-8")
    post = run(_first(ArchiveSource(archive, "favorite", None)))
    client = DetailClient()
    resolver = WeiboTextResolver(client, RequestGate(0))
    partial = run(TextCollector(resolver).collect(post, TextOptions(require_full_text=False), "missing"))
    assert partial.status == "succeeded" and not partial.content.complete
    full = run(TextCollector(resolver).collect(post, TextOptions(), "missing"))
    assert full.status == "succeeded" and full.content.text == "Hello\nworld"
    assert client.calls == ["123"]


def test_refresh_policy_ignores_a_complete_archived_text_snapshot():
    post = PostRef("123", text=PostText("123", "old body", "old body", True))
    client = DetailClient({"id": "123", "text": "refreshed body", "pics": []})
    collector = TextCollector(WeiboTextResolver(client, RequestGate(0)))
    result = run(collector.collect(post, TextOptions(), "refresh"))
    assert result.content.text == "refreshed body" and client.calls == ["123"]


async def _first(source):
    async for batch in source.discover():
        if batch.posts:
            return batch.posts[0]
    raise AssertionError("archive fixture was empty")


@pytest.mark.parametrize("raw,expected_status,expected_code", [
    ({"id": "999", "text": "wrong"}, "failed", "detail_unavailable"),
    ({"id": "123", "text": "", "pics": []}, "no_content", None),
])
def test_invalid_identity_and_confirmed_empty_text_are_distinct(raw, expected_status, expected_code):
    client = DetailClient(raw)
    result = run(TextCollector(WeiboTextResolver(client, RequestGate(0))).collect(
        PostRef("123"), TextOptions(), "missing"))
    assert result.error_code == expected_code
    assert result.status == expected_status


def test_access_restriction_is_kept_as_restricted():
    client = DetailClient(error=CollectionError("login_required"))
    result = run(TextCollector(WeiboTextResolver(client, RequestGate(0))).collect(
        PostRef("123"), TextOptions(), "missing"))
    assert result.status == "restricted" and result.error_code == "login_required"


def test_selected_text_and_images_share_one_detail_response(tmp_path):
    client = DetailClient({"id": "123", "text": "Body", "pics": []})
    shared = CachedDetailClient(client)
    image = ImageCollector(WeiboImageResolver(shared, RequestGate(0)), PassiveDownloader(),
                           FileAssetRepository(tmp_path / "images"), "scope")
    text = TextCollector(WeiboTextResolver(shared, RequestGate(0)))
    request = CrawlRequest.model_validate({
        "source": {"type": "detail", "note_ids": ["123"]},
        "collect": {"images": {}, "text": {}},
    })
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    report = run(TaskRunner(repository, "scope").run(
        CollectionPlanner((image, text)).plan(request), DetailSource(("123",))))
    assert report["status"] == "succeeded"
    assert client.calls == ["123"]
    assert {item["name"]: item["status"] for item in report["capabilities"]} == {
        "images": "no_content", "text": "succeeded",
    }


def test_discovered_text_snapshot_survives_task_recovery(tmp_path):
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    post = PostRef("123", text=PostText("123", "body", "<p>body</p>", False))
    repository.create("{}", "scope", "manual")
    with repository._connect() as db:
        task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
    repository.discover(task_id, post)
    assert repository.pending(task_id) == [post]


def test_cli_can_collect_archived_text_without_network_or_image_files(tmp_path):
    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "favorite_contents_sample.jsonl").write_text(json.dumps({
        "note_id": "123", "content": "known body", "raw_text": "known body"
    }) + "\n", encoding="utf-8")
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({
        "source": {"type": "archive", "collection": "favorite"},
        "collect": {"text": {"require_full_text": False}},
    }), encoding="utf-8")
    workspace = tmp_path / "workspace"
    result = subprocess.run([
        sys.executable, str(root / "scripts/collect_weibo.py"), "--request-file", str(request_file),
        "--archive-dir", str(archive), "--workspace", str(workspace), "--account-scope", "test",
    ], cwd=root, capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["results"][0]["content"]["text"] == "known body"
    assert report["results"][0]["content"]["complete"] is False
    assert not (workspace / "images").exists()
    assert "known body" in Path(report["outputs"]["gallery"]).read_text(encoding="utf-8")


def test_text_options_reject_unknown_and_non_boolean_fields():
    with pytest.raises(ValidationError):
        TextOptions.model_validate({"unknown": True})
    with pytest.raises(ValidationError):
        TextOptions.model_validate({"require_full_text": "false"})
