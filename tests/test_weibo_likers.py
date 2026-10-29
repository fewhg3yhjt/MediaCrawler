# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_likers.py
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

"""Offline contract and end-to-end tests for the independent likers capability."""

import asyncio
import importlib.util
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from media_platform.weibo.collection.adapters import RequestGate
from media_platform.weibo.collection.likers import LikersCollector, WeiboLikersResolver
from media_platform.weibo.collection.models import (
    CollectionError,
    LikersOptions,
    PostRef,
)
from media_platform.weibo.collection.session import decode_likers_page


def run(coro):
    return asyncio.run(coro)


class PageClient:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    async def get_note_likers_page(self, note_id, page):
        self.calls.append((note_id, page))
        return self.pages[page]


def users(*values):
    return [{"idstr": user_id, "screen_name": user_name} for user_id, user_name in values]


def test_resolver_deduplicates_users_and_stops_when_total_is_reached():
    client = PageClient({
        1: {"items": users(("10", "Ada"), ("11", "Lin")), "total": 3},
        2: {"items": users(("11", "Lin"), ("12", "Mei")), "total": 3},
    })
    collector = LikersCollector(WeiboLikersResolver(client, RequestGate(0)))

    result = run(collector.collect(PostRef("123"), LikersOptions(max_count=10), "missing"))

    assert result.status == "succeeded"
    assert result.error_code is None
    assert [item.user_id for item in result.liker_batch.items] == ["10", "11", "12"]
    assert [item.rank for item in result.liker_batch.items] == [1, 2, 3]
    assert result.liker_batch.complete and result.liker_batch.total_available == 3
    assert client.calls == [("123", 1), ("123", 2)]


def test_resolver_marks_requested_cap_as_partial_when_more_likers_are_known():
    client = PageClient({1: {"items": users(("10", "Ada"), ("11", "Lin")), "total": 8}})
    result = run(LikersCollector(WeiboLikersResolver(client, RequestGate(0))).collect(
        PostRef("123"), LikersOptions(max_count=2), "missing",
    ))

    assert result.status == "partial"
    assert result.error_code == "likers_limit_reached"
    assert len(result.liker_batch.items) == 2
    assert not result.liker_batch.complete
    assert client.calls == [("123", 1)]


def test_resolver_never_returns_more_than_requested_cap_from_a_large_page():
    client = PageClient({1: {"items": users(
        ("10", "Ada"), ("11", "Lin"), ("12", "Mei"), ("13", "Jun"),
    ), "total": 20}})
    result = run(LikersCollector(WeiboLikersResolver(client, RequestGate(0))).collect(
        PostRef("123"), LikersOptions(max_count=2), "missing",
    ))

    assert result.status == "partial"
    assert len(result.liker_batch.items) == 2
    assert not result.liker_batch.complete


@pytest.mark.parametrize("response,code", [
    ({"items": [], "total": 4}, "empty_likers_page_unverified"),
    ({"items": [{"screen_name": "missing id"}], "total": 1}, "invalid_likers_page"),
])
def test_invalid_or_unverified_pages_are_not_reported_as_empty(response, code):
    client = PageClient({1: response})
    result = run(LikersCollector(WeiboLikersResolver(client, RequestGate(0))).collect(
        PostRef("123"), LikersOptions(), "missing",
    ))

    assert result.status == "failed"
    assert result.error_code == code
    assert result.liker_batch is None


def test_duplicate_page_is_rejected_and_options_are_bounded():
    same_page = {"items": users(("10", "Ada"), ("11", "Lin")), "total": 20}
    client = PageClient({1: same_page, 2: same_page})
    result = run(LikersCollector(WeiboLikersResolver(client, RequestGate(0))).collect(
        PostRef("123"), LikersOptions(max_count=10), "missing",
    ))

    assert result.status == "failed" and result.error_code == "likers_page_repeated"
    assert client.calls == [("123", 1), ("123", 2)]
    with pytest.raises(ValidationError):
        LikersOptions(max_count=501)


@pytest.mark.parametrize("status,payload,code", [
    (403, {"ok": 0}, "access_restricted"),
    (200, {"ok": 0, "msg": "login required"}, "login_required"),
    (200, {"ok": 1, "data": {"list": []}}, "invalid_likers_page"),
    (200, {"ok": 1, "data": {"attitudes": users(("10", "Ada")), "total_number": 0}}, "invalid_likers_page"),
])
def test_decoder_preserves_access_and_schema_errors(status, payload, code):
    with pytest.raises(CollectionError, match=code):
        decode_likers_page(status, "https://m.weibo.cn/api/attitudes/show", payload)


def test_cli_request_collects_only_likers_with_fixed_http_response(tmp_path, monkeypatch, capsys):
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("collect_weibo_likers_test", root / "scripts/collect_weibo.py")
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    request_file = tmp_path / "likers-request.json"
    request_file.write_text(json.dumps({
        "source": {"type": "detail", "note_ids": ["123"]},
        "collect": {"likers": {"max_count": 20}},
    }), encoding="utf-8")
    requests = []

    def respond(request):
        requests.append(request)
        assert request.url.host == "m.weibo.cn"
        assert request.url.path == "/api/attitudes/show"
        assert request.url.params["id"] == "123"
        assert request.url.params["page"] == "1"
        return httpx.Response(200, json={"ok": 1, "data": {
            "attitudes": users(("10", "Ada")), "total_number": 1,
        }})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw,
    ))
    args = SimpleNamespace(request_file=request_file, url=None, metadata_only=False,
                           include_retweeted=False, cookie_file=None, browser_profile=None,
                           interval=0, workspace=tmp_path / "work", account_scope="test",
                           resume_task=None, archive_dir=tmp_path / "archive")

    assert run(cli.execute(args)) == 0
    report = json.loads(capsys.readouterr().out)
    result = report["results"][0]
    assert report["status"] == "succeeded"
    assert result["capability"] == "likers"
    assert result["liker_batch"]["items"] == [{"user_id": "10", "user_name": "Ada", "rank": 1}]
    assert len(requests) == 1
    assert "点赞用户" in Path(report["outputs"]["gallery"]).read_text(encoding="utf-8")


def test_web_executor_registers_likers_with_fixed_http_response(tmp_path, monkeypatch):
    from media_platform.weibo.collection import runtime
    from media_platform.weibo.collection.web_jobs import ImageWebJob

    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": 1, "data": {
            "attitudes": users(("10", "Ada")), "total_number": 1,
        }})

    real_client = httpx.AsyncClient
    monkeypatch.setattr(runtime.httpx, "AsyncClient", lambda **kw: real_client(
        transport=httpx.MockTransport(respond), **kw,
    ))

    async def execute():
        executor = runtime.WebImageExecutor(tmp_path / "work", tmp_path / "profile", "test", interval=0)
        job = ImageWebJob("job-1", {
            "source": {"type": "detail", "note_ids": ["123"]},
            "collect": {"likers": {"max_count": 20}},
        }, "123")
        return await executor.execute(job, lambda event: None, lambda content: None, login=False)

    report = run(execute())
    assert report["status"] == "succeeded"
    assert report["results"][0]["capability"] == "likers"
    assert report["results"][0]["liker_batch"]["items"][0]["user_id"] == "10"
    assert len(requests) == 1
    assert requests[0].url.host == "m.weibo.cn"
    assert requests[0].url.path == "/api/attitudes/show"
