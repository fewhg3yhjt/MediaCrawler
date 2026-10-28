# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\ports.py
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

"""Replaceable strategies and infrastructure ports, without global configuration."""

from pathlib import Path
from typing import AsyncIterator, Protocol

from pydantic import BaseModel

from .models import (
    CollectionResult, DownloadedAsset, MediaAsset, ImageManifest,
    ImageOptions, TextOptions, PostRef, PostText,
    ProgressEvent,
)


class PostSource(Protocol):
    def discover(self) -> AsyncIterator[PostRef]: ...


class ProgressObserver(Protocol):
    def __call__(self, event: ProgressEvent) -> None: ...


class ImageResolver(Protocol):
    async def resolve(self, post: PostRef, options: ImageOptions) -> ImageManifest: ...


class TextResolver(Protocol):
    async def resolve(self, post: PostRef, options: TextOptions) -> PostText: ...


class LongTextSource(Protocol):
    async def get_full_text_by_id(self, note_id: str) -> dict: ...


class AssetDownloader(Protocol):
    async def download(self, asset: MediaAsset) -> DownloadedAsset: ...


class BinaryTransport(Protocol):
    async def fetch(self, url: str) -> bytes: ...


class AssetValidator(Protocol):
    def validate(self, content: bytes) -> DownloadedAsset: ...


class AssetRepository(Protocol):
    def find(self, scope: str, asset: MediaAsset) -> Path | None: ...
    def save(self, scope: str, asset: MediaAsset, data: DownloadedAsset) -> Path: ...


class Collector(Protocol):
    name: str
    options_type: type[BaseModel]

    async def collect(
        self, post: PostRef, options: BaseModel, policy: str,
    ) -> CollectionResult: ...


class TaskRepository(Protocol):
    def create(self, request_json: str, scope: str, collection: str) -> str: ...
    def resume(self, task_id: str, request_json: str, scope: str) -> None: ...
    def discover(self, task_id: str, post: PostRef) -> None: ...
    def pending(self, task_id: str) -> list[PostRef]: ...
    def completed(self, task_id: str, post: PostRef, capability: str) -> bool: ...
    def record(
        self, task_id: str, post: PostRef, capability: str,
        options_json: str, result: CollectionResult,
    ) -> None: ...
    def finish(self, task_id: str, status: str, stop_reason: str) -> None: ...
    def report(self, task_id: str) -> dict: ...
