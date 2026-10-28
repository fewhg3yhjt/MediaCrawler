# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\models.py
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

"""Task contracts and immutable collection results."""

from dataclasses import dataclass
from typing import Annotated, Literal
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, StrictBool, field_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ImageOptions(Contract):
    download: StrictBool = False
    include_retweeted: StrictBool = False
    verify_manifest: StrictBool = False


class TextOptions(Contract):
    require_full_text: StrictBool = True


class MediaOptions(Contract):
    download: StrictBool = True
    include_retweeted: StrictBool = False


class VideoOptions(MediaOptions):
    pass


class AudioOptions(MediaOptions):
    pass


class DetailSpec(Contract):
    type: Literal["detail"]
    note_ids: tuple[str, ...] = Field(min_length=1)

    @field_validator("note_ids")
    @classmethod
    def numeric_ids(cls, value):
        from .links import WeiboPostLink
        return tuple(dict.fromkeys(WeiboPostLink.parse(item).note_id for item in value))


class CreatorSpec(Contract):
    type: Literal["creator"]
    creator_id: str

    @field_validator("creator_id")
    @classmethod
    def numeric_creator_id(cls, value):
        from .links import WeiboCreatorLink
        return WeiboCreatorLink.parse(value).creator_id


class ArchiveSpec(Contract):
    type: Literal["archive"]
    collection: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    note_ids: tuple[str, ...] | None = Field(default=None, min_length=1)

    @field_validator("note_ids")
    @classmethod
    def numeric_ids(cls, value):
        return None if value is None else DetailSpec.numeric_ids(value)


class UpdateSpec(Contract):
    default: Literal["missing", "refresh"] = "missing"


class Limits(Contract):
    max_posts: int = Field(default=1000, gt=0, le=10000, strict=True)


class OutputSpec(Contract):
    collection: str | None = Field(default=None, pattern=r"^[A-Za-z0-9_-]+$")


class CrawlRequest(Contract):
    schema_version: Literal[1] = 1
    source: Annotated[DetailSpec | ArchiveSpec | CreatorSpec, Field(discriminator="type")]
    collect: dict[str, dict] = Field(min_length=1)
    update: UpdateSpec = Field(default_factory=UpdateSpec)
    limits: Limits = Field(default_factory=Limits)
    output: OutputSpec = Field(default_factory=OutputSpec)


@dataclass(frozen=True)
class MediaAsset:
    asset_id: str
    post_id: str
    owner_post_id: str
    url: str
    kind: Literal["image", "video", "audio"] = "image"


@dataclass(frozen=True)
class ImageManifest:
    assets: tuple[MediaAsset, ...]
    originals_complete: bool
    retweeted_complete: bool

    def satisfies(self, options: ImageOptions) -> bool:
        return self.originals_complete and (
            not options.include_retweeted or self.retweeted_complete
        )


@dataclass(frozen=True)
class MediaManifest:
    assets: tuple[MediaAsset, ...]
    originals_complete: bool
    retweeted_complete: bool

    def satisfies(self, options: MediaOptions) -> bool:
        return self.originals_complete and (not options.include_retweeted or self.retweeted_complete)


@dataclass(frozen=True)
class PostRef:
    note_id: str
    images: ImageManifest | None = None
    text: "PostText | None" = None


@dataclass(frozen=True)
class PostText:
    note_id: str
    text: str
    raw_text: str
    complete: bool
    error_code: str | None = None


@dataclass(frozen=True)
class AssetResult:
    asset: MediaAsset
    status: Literal["succeeded", "failed"]
    local_path: str | None = None
    error_code: str | None = None


@dataclass(frozen=True)
class CollectionResult:
    status: Literal["succeeded", "no_content", "partial", "failed", "restricted"]
    assets: tuple[AssetResult, ...] = ()
    error_code: str | None = None
    content: PostText | None = None


@dataclass(frozen=True)
class DownloadedAsset:
    content: bytes
    extension: str


@dataclass(frozen=True)
class DownloadedFile:
    path: Path
    extension: str


@dataclass(frozen=True)
class ProgressEvent:
    phase: str
    task_id: str | None = None
    post_id: str | None = None
    capability: str | None = None
    completed: int = 0
    total: int | None = None


class CollectionError(Exception):
    """Expected adapter failure with a safe, persistable code."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class TaskExecutionError(CollectionError):
    def __init__(self, task_id: str, code: str = "source_or_storage_failed"):
        super().__init__(code)
        self.task_id = task_id
