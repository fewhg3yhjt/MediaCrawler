# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\images.py
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

"""Image collection orchestrates image work, independent of discovery and storage."""

from .models import AssetResult, CollectionError, CollectionResult, ImageOptions, PostRef, ProgressEvent
from .ports import AssetDownloader, AssetRepository, ImageResolver, ProgressObserver


class ImageCollector:
    name = "images"
    options_type = ImageOptions

    def __init__(
        self, resolver: ImageResolver, downloader: AssetDownloader,
        assets: AssetRepository, scope: str, observer: ProgressObserver | None = None,
    ):
        self.resolver = resolver
        self.downloader = downloader
        self.assets = assets
        self.scope = scope
        self.observer = observer

    async def collect(self, post: PostRef, options: ImageOptions, policy: str) -> CollectionResult:
        try:
            manifest = await self.resolver.resolve(post, options)
        except CollectionError as exc:
            status = "restricted" if exc.code in ("login_required", "access_restricted") else "failed"
            return CollectionResult(status, error_code=exc.code)
        selected = {
            (a.owner_post_id, a.asset_id): a for a in manifest.assets
            if options.include_retweeted or a.owner_post_id == post.note_id
        }
        if self.observer:
            self.observer(ProgressEvent("images", post_id=post.note_id, total=len(selected)))
        results = []
        for asset in selected.values():
            try:
                path = None
                if options.download:
                    path = None if policy == "refresh" else self.assets.find(self.scope, asset)
                    if path is None:
                        data = await self.downloader.download(asset)
                        path = self.assets.save(self.scope, asset, data)
                results.append(AssetResult(asset, "succeeded", str(path) if path else None))
            except CollectionError as exc:
                results.append(AssetResult(asset, "failed", error_code=exc.code))
            if self.observer:
                self.observer(ProgressEvent("images", post_id=post.note_id,
                                            completed=len(results), total=len(selected)))
        failures = sum(item.status == "failed" for item in results)
        if not manifest.satisfies(options):
            status = "partial"
        elif not results:
            status = "no_content"
        elif failures:
            status = "failed" if failures == len(results) else "partial"
        else:
            status = "succeeded"
        reason = "manifest_unverified" if not manifest.satisfies(options) else None
        return CollectionResult(status, tuple(results), error_code=reason)
