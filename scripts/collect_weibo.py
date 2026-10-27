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
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx

from media_platform.weibo.client import WeiboClient
from media_platform.weibo.collection.adapters import (
    HttpBinaryTransport, ImageValidator, RequestGate, ValidatedAssetDownloader, WeiboImageResolver,
)
from media_platform.weibo.collection.images import ImageCollector
from media_platform.weibo.collection.models import CrawlRequest, TaskExecutionError
from media_platform.weibo.collection.repository import FileAssetRepository, SqliteTaskRepository
from media_platform.weibo.collection.service import CollectionPlanner, TaskRunner
from media_platform.weibo.collection.sources import ArchiveSource, DetailSource


async def execute(args):
    request = CrawlRequest.model_validate_json(args.request_file.read_text(encoding="utf-8"))
    cookies = args.cookie_file.read_text(encoding="utf-8").strip() if args.cookie_file else ""
    if "\r" in cookies or "\n" in cookies:
        raise ValueError("cookie file must contain one Cookie header value")
    gate = RequestGate(args.interval)
    client = WeiboClient(
        headers={"User-Agent": "Mozilla/5.0", "Cookie": cookies},
        playwright_page=None, cookie_dict={},
    )
    async with httpx.AsyncClient(timeout=30) as http:
        images = ImageCollector(
            WeiboImageResolver(client, gate),
            ValidatedAssetDownloader(HttpBinaryTransport(http, gate), ImageValidator()),
            FileAssetRepository(args.workspace / "images"), args.account_scope,
        )
        # Registration is the only capability support table. Repositories are
        # lazy, so planning still precedes any output files or network requests.
        plan = CollectionPlanner((images,)).plan(request)
        source = (
            DetailSource(request.source.note_ids) if request.source.type == "detail"
            else ArchiveSource(args.archive_dir, request.source.collection, request.source.note_ids)
        )
        repository = SqliteTaskRepository(args.workspace / "tasks.sqlite3")
        report = await TaskRunner(repository, args.account_scope).run(plan, source, args.resume_task)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] == "succeeded" else 2


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="微博按需采集：图片模块样板")
    parser.add_argument("--request-file", required=True, type=Path)
    parser.add_argument("--archive-dir", type=Path, default=ROOT / "data/weibo/jsonl")
    parser.add_argument("--workspace", type=Path, default=ROOT / "data/weibo/collection")
    parser.add_argument("--account-scope", required=True, help="本地账号隔离标识；不表示已验证登录身份")
    parser.add_argument("--cookie-file", type=Path, help="可选：仅含 Cookie 请求头值的本地文件")
    parser.add_argument("--interval", type=float, default=2.0)
    parser.add_argument("--resume-task", help="使用相同请求重跑，跳过已成功的能力，重试失败/部分完成项")
    args = parser.parse_args()
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
