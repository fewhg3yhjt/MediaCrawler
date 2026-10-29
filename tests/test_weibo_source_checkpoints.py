# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_source_checkpoints.py
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

"""Offline tests for durable source checkpoints and page-drift recovery."""

import asyncio

import pytest

from media_platform.weibo.collection.adapters import RequestGate
from media_platform.weibo.collection.models import (
    CollectionError,
    CollectionResult,
    CrawlRequest,
    ImageOptions,
    PostRef,
    SourceCheckpoint,
    TaskExecutionError,
)
from media_platform.weibo.collection.repository import SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner
from media_platform.weibo.collection.sources import CreatorSource, SelfFeedSource


def run(coro):
    return asyncio.run(coro)


class Recorder:
    name = "images"
    options_type = ImageOptions

    def __init__(self):
        self.ids = []

    async def collect(self, post, options, policy):
        self.ids.append(post.note_id)
        return CollectionResult("succeeded")


class CreatorClient:
    def __init__(self, pages, fail_page=None):
        self.pages = pages
        self.fail_page = fail_page
        self.calls = []

    async def get_creator_notes_page(self, creator_id, page):
        self.calls.append(page)
        if page == self.fail_page:
            raise RuntimeError("injected page failure")
        return self.pages[page]


def creator_pages(mapping):
    return {
        page: {"list": [{"id": note_id} for note_id in ids], "has_more": has_more}
        for page, (ids, has_more) in mapping.items()
    }


def request(max_posts=20):
    return CrawlRequest.model_validate({
        "source": {"type": "creator", "creator_id": "111"},
        "collect": {"images": {}},
        "limits": {"max_posts": max_posts},
    })


def build_plan(collector):
    return CollectionPlanner((collector,)).plan(request())


@pytest.mark.parametrize(
    ("last_page_ids", "expected_unresolved", "expected_status"),
    [(["5", "2"], 0, "recovered"), (["2"], 1, "unresolved")],
)
def test_resume_replays_two_pages_and_reconciles_page_drift(
    tmp_path, last_page_ids, expected_unresolved, expected_status,
):
    async def execute():
        repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
        collector = Recorder()
        plan = build_plan(collector)
        first_client = CreatorClient(
            creator_pages({
                1: (["11", "10"], True),
                2: (["9", "8"], True),
                3: (["7", "6"], True),
                4: (["5", "4"], True),
            }),
            fail_page=5,
        )
        runner = TaskRunner(repository, "test-scope")

        with pytest.raises(TaskExecutionError, match="creator_request_failed") as error:
            await runner.run(plan, CreatorSource("111", first_client, RequestGate(0)))

        task_id = error.value.task_id
        checkpoint = repository.source_checkpoint(task_id, CreatorSource("111", first_client, RequestGate(0)).checkpoint_key)
        assert checkpoint is not None
        assert checkpoint.cursor["next_page"] == 5
        assert checkpoint.cursor["pages_scanned"] == 4
        assert first_client.calls == [1, 2, 3, 4, 5]

        resumed_client = CreatorClient(creator_pages({
            3: (["7", "6"], True),
            4: (["4", "3"], True),
            5: (last_page_ids, True),
            6: ([], False),
        }))
        report = await runner.run(plan, CreatorSource("111", resumed_client, RequestGate(0)), task_id)
        return report, resumed_client, collector

    report, client, collector = run(execute())
    assert client.calls == [3, 4, 5, 6]
    assert report["status"] == "succeeded"
    assert report["source"]["overlap_verified"] is True
    assert report["source"]["changed_pages"] == 1
    assert report["source"]["pending_gap_count"] == 0
    assert report["source"]["unresolved_gap_count"] == expected_unresolved
    assert report["source"]["page_drift"][0]["status"] == expected_status
    assert report["source"]["page_drift"][0]["missing_ids"] == ["5"]
    assert "3" in collector.ids
    assert "2" in collector.ids
    assert collector.ids.count("7") == 1


def test_current_account_checkpoint_rejects_a_different_uid_before_feed_read():
    class SelfClient:
        def __init__(self):
            self.uid_calls = 0
            self.feed_calls = 0

        async def get_current_user_uid(self):
            self.uid_calls += 1
            return "222"

        async def get_self_feed_page(self, feed_type, uid, page):
            self.feed_calls += 1
            return {"list": [], "has_more": False}

    async def execute():
        client = SelfClient()
        source = SelfFeedSource("favorite", client, RequestGate(0))
        checkpoint = SourceCheckpoint(source.checkpoint_key, {
            "uid": "111",
            "next_page": 2,
            "pages_scanned": 1,
            "empty_pages": 0,
            "last_page_ids": ["1"],
        })
        with pytest.raises(CollectionError, match="source_identity_changed"):
            await source.validate_checkpoint(checkpoint)
        return client

    client = run(execute())
    assert client.uid_calls == 1
    assert client.feed_calls == 0


def test_max_posts_inside_page_processes_eligible_posts_without_advancing_checkpoint(tmp_path):
    async def execute():
        repository = SqliteTaskRepository(tmp_path / "tasks.sqlite3")
        collector = Recorder()
        plan = CollectionPlanner((collector,)).plan(request(max_posts=1))
        client = CreatorClient(creator_pages({1: (["1", "2"], False)}))
        report = await TaskRunner(repository, "test-scope").run(
            plan, CreatorSource("111", client, RequestGate(0)),
        )
        return report, client, collector

    report, client, collector = run(execute())
    assert client.calls == [1]
    assert collector.ids == ["1"]
    assert report["status"] == "partial"
    assert report["stop_reason"] == "limit_reached"
    assert report["discovered"] == 1
    assert report["source"] is None
