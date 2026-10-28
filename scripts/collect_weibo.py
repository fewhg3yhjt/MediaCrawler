# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/scripts\collect_weibo.py
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

"""Composition root for the new collection architecture (image pilot)."""

import argparse
import asyncio
import json
import sys
from contextlib import AsyncExitStack
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from media_platform.weibo.collection.adapters import (
    AudioFileValidator, CachedDetailClient, HttpBinaryTransport, ImageValidator, RequestGate,
    StreamingAssetDownloader, ValidatedAssetDownloader, VideoFileValidator, WeiboImageResolver,
)
from media_platform.weibo.collection.images import ImageCollector
from media_platform.weibo.collection.text import TextCollector, WeiboTextResolver
from media_platform.weibo.collection.media import MediaCollector, WeiboMediaResolver
from media_platform.weibo.collection.application import CollectionApplication, PostSourceFactory
from media_platform.weibo.collection.models import AudioOptions, CrawlRequest, TaskExecutionError, VideoOptions
from media_platform.weibo.collection.repository import FileAssetRepository, SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner, validate_capabilities
from media_platform.weibo.collection.presentation import ImageReportWriter
from media_platform.weibo.collection.session import BrowserProfileSession, HttpWeiboDetailClient


async def execute(args):
    request = (
        CrawlRequest.model_validate_json(args.request_file.read_text(encoding="utf-8"))
        if args.request_file else CrawlRequest.model_validate({
            "source": {"type": "detail", "note_ids": [args.url]},
            "collect": {"images": {"download": not args.metadata_only,
                                    "include_retweeted": args.include_retweeted}},
        })
    )
    supported = {
        ImageCollector.name: ImageCollector.options_type,
        TextCollector.name: TextCollector.options_type,
        "video": VideoOptions,
        "audio": AudioOptions,
    }
    validate_capabilities(request, supported)
    cookies = args.cookie_file.read_text(encoding="utf-8").strip() if args.cookie_file else ""
    if "\r" in cookies or "\n" in cookies:
        raise ValueError("cookie file must contain one Cookie header value")
    gate = RequestGate(args.interval)
    async with AsyncExitStack() as stack:
        http = await stack.enter_async_context(httpx.AsyncClient(timeout=30))
        detail_http = await stack.enter_async_context(httpx.AsyncClient(
            timeout=30, headers={"User-Agent": "Mozilla/5.0", "Cookie": cookies},
        ))
        client = HttpWeiboDetailClient(detail_http)
        if args.browser_profile:
            client = await stack.enter_async_context(BrowserProfileSession(args.browser_profile).open())
        client = CachedDetailClient(client)
        images = ImageCollector(
            WeiboImageResolver(client, gate),
            ValidatedAssetDownloader(HttpBinaryTransport(http, gate), ImageValidator()),
            FileAssetRepository(args.workspace / "images"), args.account_scope,
        )
        text = TextCollector(WeiboTextResolver(client, gate, client))
        video = MediaCollector(
            "video", WeiboMediaResolver(client, gate, "video"),
            StreamingAssetDownloader(HttpBinaryTransport(http, gate), VideoFileValidator(),
                                     args.workspace / "video", 1024 * 1024 * 1024),
            FileAssetRepository(args.workspace / "video"), args.account_scope, VideoOptions,
        )
        audio = MediaCollector(
            "audio", WeiboMediaResolver(client, gate, "audio"),
            StreamingAssetDownloader(HttpBinaryTransport(http, gate), AudioFileValidator(),
                                     args.workspace / "audio", 100 * 1024 * 1024),
            FileAssetRepository(args.workspace / "audio"), args.account_scope, AudioOptions,
        )
        # Registration is the only capability support table. Repositories are
        # lazy, so planning still precedes any output files or network requests.
        repository = SqliteTaskRepository(args.workspace / "tasks.sqlite3")
        application = CollectionApplication(
            CollectionPlanner((images, text, video, audio)), TaskRunner(repository, args.account_scope),
            PostSourceFactory(args.archive_dir), ImageReportWriter(args.workspace / "reports"),
        )
        report = await application.execute(request, args.resume_task)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "succeeded" else 2


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="微博按需采集：正文、图片、视频与音频能力")
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--request-file", type=Path)
    inputs.add_argument("--url", help="单条微博链接或微博 ID，默认下载图片")
    parser.add_argument("--metadata-only", action="store_true", help="链接模式下只解析图片地址")
    parser.add_argument("--include-retweeted", action="store_true", help="链接模式下包含转发原文图片")
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data/weibo/jsonl")
    parser.add_argument("--workspace", type=Path, default=ROOT / "data/weibo/collection")
    parser.add_argument("--account-scope", required=True, help="本地账号隔离标识；不表示已验证登录身份")
    parser.add_argument("--cookie-file", type=Path, help="可选：仅含 Cookie 请求头值的本地文件")
    parser.add_argument("--browser-profile", type=Path, help="显式使用指定 Chrome 登录目录；仅经授权使用，不自动扫码")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--resume-task", help="使用相同请求重跑，跳过已成功的能力，重试失败/部分完成项")
    args = parser.parse_args()
    if args.request_file and (args.metadata_only or args.include_retweeted):
        parser.error("请求文件模式请在 collect.images 中设置图片选项")
    if args.cookie_file and args.browser_profile:
        parser.error("cookie-file 与 browser-profile 不能同时使用")
    try:
        return asyncio.run(execute(args))
    except (ValueError, OSError) as exc:
        print(f"请求或本地文件无效：{exc}", file=sys.stderr)
        return 1
    except TaskExecutionError as exc:
        print(f"任务失败，任务 ID：{exc.task_id}；原因：{exc.code}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130
    except Exception:
        # Third-party client exceptions may include authentication headers.
        print("任务执行失败；请检查请求、来源、登录状态和任务状态库。", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
