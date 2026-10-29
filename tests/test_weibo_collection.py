# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_collection.py
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

"""Behavioral tests for the image pilot, with no live requests or global config."""

import asyncio
import io
import json
from pathlib import Path

import httpx
import pytest
from PIL import Image
from pydantic import ValidationError

from media_platform.weibo.collection.adapters import (
    HttpBinaryTransport, ImageValidator, RequestGate, ValidatedAssetDownloader, WeiboImageResolver,
)
from media_platform.weibo.collection.images import ImageCollector
from media_platform.weibo.collection.models import (
    CollectionError, CollectionResult, CrawlRequest, DownloadedAsset,
    MediaAsset, ImageManifest, ImageOptions, PostRef, TaskExecutionError,
)
from media_platform.weibo.collection.repository import FileAssetRepository, SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner
from media_platform.weibo.collection.sources import ArchiveSource, DetailSource


def run(coro):
    return asyncio.run(coro)


def request(**changes):
    values = {"source": {"type": "detail", "note_ids": ["123"]}, "collect": {"images": {"download": True}}}
    values.update(changes)
    return CrawlRequest.model_validate(values)


def png():
    stream = io.BytesIO()
    Image.new("RGB", (2, 2), "red").save(stream, format="PNG")
    return stream.getvalue()


class Client:
    def __init__(self, raw=None):
        self.calls = []
        self.raw = raw if raw is not None else {
            "id": "123", "pics": [
                {"pid": "a", "large": {"url": "https://wx1.sinaimg.cn/large/a.jpg"}},
                {"pid": "b", "large": {"url": "https://wx1.sinaimg.cn/large/b.jpg"}},
            ],
        }

    async def get_note_info_by_id(self, note_id):
        self.calls.append(note_id)
        return {"mblog": self.raw}


class Downloader:
    def __init__(self):
        self.calls = []
        self.fail = set()

    async def download(self, asset):
        self.calls.append(asset.asset_id)
        if asset.asset_id in self.fail:
            raise CollectionError("image_request_failed")
        return DownloadedAsset(png(), "png")


def setup_collector(tmp_path, client=None):
    client = client or Client()
    downloader = Downloader()
    assets = FileAssetRepository(tmp_path / "images")
    collector = ImageCollector(WeiboImageResolver(client, RequestGate(0)), downloader, assets, "account-a")
    return collector, client, downloader, assets


def test_plan_rejects_unsupported_and_invalid_options(tmp_path):
    collector, client, downloader, _ = setup_collector(tmp_path)
    planner = CollectionPlanner((collector,))
    with pytest.raises(ValueError, match="unsupported capabilities"):
        planner.plan(request(collect={"likers": {}}))
    with pytest.raises(ValidationError):
        planner.plan(request(collect={"images": {"download": "false"}}))
    with pytest.raises(ValidationError):
        planner.plan(request(collect={"images": {"unknown": True}}))
    assert not client.calls and not downloader.calls


def test_metadata_only_never_downloads(tmp_path):
    collector, client, downloader, _ = setup_collector(tmp_path)
    result = run(collector.collect(PostRef("123"), ImageOptions(), "missing"))
    assert result.status == "succeeded"
    assert len(result.assets) == 2
    assert all(item.local_path is None for item in result.assets)
    assert client.calls == ["123"] and downloader.calls == []


def test_failed_asset_only_is_downloaded_on_retry(tmp_path):
    collector, client, downloader, _ = setup_collector(tmp_path)
    downloader.fail = {"b"}
    first = run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    assert first.status == "partial"
    downloader.fail.clear()
    second = run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    assert second.status == "succeeded"
    assert downloader.calls == ["a", "b", "b"]
    assert Path(second.assets[0].local_path).exists()


def test_metadata_success_does_not_satisfy_download(tmp_path):
    collector, _, downloader, _ = setup_collector(tmp_path)
    run(collector.collect(PostRef("123"), ImageOptions(), "missing"))
    result = run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    assert result.status == "succeeded" and downloader.calls == ["a", "b"]


def test_corrupted_file_is_downloaded_again_and_scopes_are_isolated(tmp_path):
    collector, _, downloader, assets = setup_collector(tmp_path)
    first = run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    # Both images share identical bytes; the content addressed file is shared.
    Path(first.assets[0].local_path).write_bytes(b"corrupt")
    second = run(collector.collect(PostRef("123"), ImageOptions(download=True), "missing"))
    assert second.status == "succeeded"
    assert downloader.calls == ["a", "b", "a"]
    assert assets.find("account-b", first.assets[0].asset) is None


def test_archive_and_detail_reuse_same_collector_without_detail_request(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    record = {"note_id": "123", "image_urls": '["https://wx1.sinaimg.cn/large/a.jpg"]',
              "retweeted_note_id": "456", "rt_images": '["https://wx1.sinaimg.cn/large/r.jpg"]'}
    (archive / "favorite_contents_2026-10-27.jsonl").write_text(json.dumps(record) + "\n", encoding="utf-8")
    collector, client, downloader, _ = setup_collector(tmp_path)

    async def execute():
        batches = [batch async for batch in ArchiveSource(archive, "favorite", None).discover()]
        posts = [post for batch in batches for post in batch.posts]
        return await collector.collect(posts[0], ImageOptions(download=True, include_retweeted=True), "missing")

    result = run(execute())
    assert result.status == "partial"  # Legacy values cannot prove completeness.
    assert {a.asset.owner_post_id for a in result.assets} == {"123", "456"}
    assert not client.calls and downloader.calls == ["a", "r"]


def test_verify_archive_manifest_fetches_detail(tmp_path):
    collector, client, _, _ = setup_collector(tmp_path)
    asset = MediaAsset("old", "123", "123", "https://wx1.sinaimg.cn/large/old.jpg")
    post = PostRef("123", ImageManifest((asset,), False, False))
    result = run(collector.collect(post, ImageOptions(verify_manifest=True), "missing"))
    assert client.calls == ["123"] and result.status == "succeeded"
    assert {a.asset.asset_id for a in result.assets} == {"a", "b"}


def test_empty_legacy_field_is_not_proof_of_no_images(tmp_path):
    collector, client, _, _ = setup_collector(tmp_path)
    post = PostRef("123", ImageManifest((), False, False))
    result = run(collector.collect(post, ImageOptions(), "missing"))
    assert client.calls == ["123"] and len(result.assets) == 2


@pytest.mark.parametrize("raw,expected", [
    ({"id": "123", "pics": []}, "no_content"),
    ({"id": "123", "pic_num": 2}, "partial"),
    ({"id": "123"}, "partial"),
    ({"id": "999", "pics": []}, "failed"),
    ({"id": "123", "deleted": True}, "failed"),
])
def test_empty_incomplete_and_unavailable_detail_are_distinct(tmp_path, raw, expected):
    collector, _, _, _ = setup_collector(tmp_path, Client(raw))
    assert run(collector.collect(PostRef("123"), ImageOptions(), "missing")).status == expected


def test_pc_response_and_retweet_selection(tmp_path):
    raw = {"id": "123", "pic_ids": ["a"], "pic_infos": {"a": {"pid": "a", "large": {"url": "https://wx1.sinaimg.cn/large/a.jpg"}}},
           "retweeted_status": {"id": "456", "pics": ["https://wx1.sinaimg.cn/large/r.jpg"]}}
    collector, _, _, _ = setup_collector(tmp_path, Client(raw))
    own = run(collector.collect(PostRef("123"), ImageOptions(), "missing"))
    both = run(collector.collect(PostRef("123"), ImageOptions(include_retweeted=True), "missing"))
    assert len(own.assets) == 1 and len(both.assets) == 2


def test_task_recovery_persists_work_and_collection_memberships(tmp_path):
    collector, client, downloader, _ = setup_collector(tmp_path)
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    planner = CollectionPlanner((collector,))
    plan = planner.plan(request())
    downloader.fail = {"b"}
    runner = TaskRunner(repository, "account-a")
    first = run(runner.run(plan, DetailSource(("123",))))
    assert first["status"] == "partial"
    downloader.fail.clear()
    second = run(runner.run(plan, DetailSource(("123",)), first["task_id"]))
    assert second["status"] == "succeeded" and downloader.calls == ["a", "b", "b"]
    before = len(client.calls)
    run(runner.run(plan, DetailSource(("123",)), first["task_id"]))
    assert len(client.calls) == before
    other = planner.plan(request(output={"collection": "favorite"}))
    run(runner.run(other, DetailSource(("123",))))
    with repository._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM collection_items WHERE note_id='123'").fetchone()[0] == 2
    with pytest.raises(ValueError, match="identical request"):
        run(runner.run(other, DetailSource(("123",)), first["task_id"]))


def test_cancelled_task_keeps_discovered_work(tmp_path):
    class CancelCollector:
        name, options_type = "images", ImageOptions

        async def collect(self, post, options, policy):
            raise asyncio.CancelledError()

    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    plan = CollectionPlanner((CancelCollector(),)).plan(request())
    with pytest.raises(asyncio.CancelledError):
        run(TaskRunner(repository, "account-a").run(plan, DetailSource(("123",))))
    with repository._connect() as db:
        task_id = db.execute("SELECT id FROM tasks").fetchone()[0]
    assert repository.report(task_id)["status"] == "cancelled"
    assert repository.pending(task_id) == [PostRef("123")]


def test_limits_and_duplicate_source_ids(tmp_path):
    collector, client, _, _ = setup_collector(tmp_path)
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    plan = CollectionPlanner((collector,)).plan(request(limits={"max_posts": 1}))
    report = run(TaskRunner(repository, "account-a").run(plan, DetailSource(("123", "123", "456"))))
    assert report["discovered"] == 1 and report["stop_reason"] == "limit_reached"
    assert client.calls == ["123"]


@pytest.mark.parametrize("body,code", [(b"<html>login</html>", "invalid_image_content"), (b"x" * 101, "asset_too_large")])
def test_downloader_rejects_nonimage_and_oversized_responses(body, code):
    async def execute():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=body))
        async with httpx.AsyncClient(transport=transport) as client:
            downloader = ValidatedAssetDownloader(HttpBinaryTransport(client, RequestGate(0), max_bytes=100), ImageValidator())
            return await downloader.download(MediaAsset("a", "123", "123", "https://wx1.sinaimg.cn/large/a.jpg"))
    with pytest.raises(CollectionError, match=code):
        run(execute())


def test_downloader_validates_file_format_instead_of_url_extension():
    async def execute():
        transport = httpx.MockTransport(lambda request: httpx.Response(200, content=png()))
        async with httpx.AsyncClient(transport=transport) as client:
            return await ValidatedAssetDownloader(HttpBinaryTransport(client, RequestGate(0)), ImageValidator()).download(
                MediaAsset("a", "123", "123", "https://wx1.sinaimg.cn/large/a.jpg?token=shortlived")
            )
    assert run(execute()).extension == "png"


def test_unselected_collector_and_independent_failure(tmp_path):
    class OtherCollector:
        name, options_type = "other", ImageOptions

        async def collect(self, post, options, policy):
            raise RuntimeError("failure isolated from images")

    collector, client, _, _ = setup_collector(tmp_path)
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    planner = CollectionPlanner((collector, OtherCollector()))
    other_only = planner.plan(request(collect={"other": {}}))
    report = run(TaskRunner(repository, "account-a").run(other_only, DetailSource(("123",))))
    assert not client.calls and report["capabilities"][0]["status"] == "failed"
    both = planner.plan(request(collect={"other": {}, "images": {}}))
    report = run(TaskRunner(repository, "account-a").run(both, DetailSource(("123",))))
    assert {r["name"]: r["status"] for r in report["capabilities"]} == {"images": "succeeded", "other": "failed"}


def test_refresh_downloads_again_and_plan_options_are_snapshotted(tmp_path):
    collector, _, downloader, _ = setup_collector(tmp_path)
    original = request(update={"default": "refresh"})
    plan = CollectionPlanner((collector,)).plan(original)
    original.collect["images"]["download"] = False
    assert plan.steps[0].options.download is True
    assert json.loads(plan.request_json)["collect"]["images"]["download"] is True
    for _ in range(2):
        run(collector.collect(PostRef("123"), plan.steps[0].options, plan.policy))
    assert downloader.calls == ["a", "b", "a", "b"]


def test_source_failure_has_safe_task_id_and_persisted_reason(tmp_path):
    collector, _, _, _ = setup_collector(tmp_path)
    repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
    plan = CollectionPlanner((collector,)).plan(request())
    with pytest.raises(TaskExecutionError) as raised:
        run(TaskRunner(repository, "account-a").run(plan, ArchiveSource(tmp_path, "absent", None)))
    assert raised.value.code == "archive_collection_not_found"
    assert repository.report(raised.value.task_id)["status"] == "failed"


def test_cli_offline_archive_and_invalid_request_have_no_downloads(tmp_path):
    import subprocess
    import sys

    root = Path(__file__).resolve().parents[1]
    archive = tmp_path / "archive"
    archive.mkdir()
    (archive / "favorite_contents_sample.jsonl").write_text(json.dumps({
        "note_id": "123", "image_urls": '["https://wx1.sinaimg.cn/large/a.jpg"]'
    }) + "\n", encoding="utf-8")
    request_file = tmp_path / "request.json"
    request_file.write_text(json.dumps({"source": {"type": "archive", "collection": "favorite"},
                                        "collect": {"images": {"download": False}}}), encoding="utf-8")
    workspace = tmp_path / "work"
    command = [sys.executable, str(root / "scripts/collect_weibo.py"), "--request-file", str(request_file),
               "--archive-dir", str(archive), "--workspace", str(workspace), "--account-scope", "test"]
    completed = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
    assert completed.returncode == 2, completed.stderr
    report = json.loads(completed.stdout)
    assert report["discovered"] == 1 and report["status"] == "partial"
    assert not (workspace / "images").exists()
    request_file.write_text(json.dumps({"source": {"type": "detail", "note_ids": ["123"]},
                                        "collect": {"likers": {"max_count": 501}}}), encoding="utf-8")
    rejected_work = tmp_path / "rejected"
    command[command.index("--workspace") + 1] = str(rejected_work)
    rejected = subprocess.run(command, cwd=root, capture_output=True, timeout=30)
    assert rejected.returncode == 1 and not rejected_work.exists()


def test_domain_import_does_not_load_legacy_browser_stack():
    import subprocess
    import sys

    code = ("import sys; import media_platform.weibo.collection.service; "
            "assert 'media_platform.weibo.core' not in sys.modules; "
            "assert 'database.models' not in sys.modules")
    subprocess.run([sys.executable, "-c", code], check=True, timeout=30,
                   cwd=Path(__file__).resolve().parents[1])


def test_download_algorithm_reuses_transport_with_different_validator(tmp_path):
    class Transport:
        async def fetch(self, url):
            assert url == "https://example.org/clip"
            return b"sample-video-payload"

    class Validator:
        def validate(self, content):
            return DownloadedAsset(content, "mp4")

    # This proves mechanism reuse; it does not register a video collector.
    asset = MediaAsset("a", "123", "123", "https://example.org/clip", kind="video")
    data = run(ValidatedAssetDownloader(Transport(), Validator()).download(asset))
    repository = FileAssetRepository(tmp_path / "assets")
    path = repository.save("account-a", asset, data)
    assert repository.find("account-a", asset) == path
    assert path.suffix == ".mp4"
    image = MediaAsset("a", "123", "123", "https://example.org/image", kind="image")
    assert repository.find("account-a", image) is None
