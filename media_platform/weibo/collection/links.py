# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\links.py
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

"""Normalize supported Weibo post links without making network requests."""

import re
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class WeiboPostLink:
    note_id: str

    def __post_init__(self):
        if int(self.note_id) <= 0:
            raise ValueError("invalid Weibo post identifier")

    @classmethod
    def parse(cls, value: str) -> "WeiboPostLink":
        value = value.strip()
        if re.fullmatch(r"[0-9]+", value):
            return cls(str(int(value)))
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("weibo.com", "www.weibo.com", "m.weibo.cn")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError("unsupported Weibo post link")
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2:
            raise ValueError("expected a single Weibo post link")
        prefix, token = parts
        if prefix not in ("detail", "status") and not re.fullmatch(r"[0-9]+", prefix):
            raise ValueError("expected a single Weibo post link")
        if re.fullmatch(r"[0-9]+", token) and (prefix == "detail" or len(token) >= 15):
            return cls(str(int(token)))
        if prefix == "detail" or not re.fullmatch(r"[0-9a-zA-Z]{1,12}", token):
            raise ValueError("invalid Weibo post identifier")
        alphabet = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        chunks = []
        for end in range(len(token), 0, -4):
            number = 0
            for char in token[max(0, end - 4):end]:
                number = number * 62 + alphabet.index(char)
            if number >= 10_000_000:
                raise ValueError("invalid Weibo post identifier")
            chunks.append(str(number).zfill(7) if end > 4 else str(number))
        return cls(str(int("".join(reversed(chunks)))))


@dataclass(frozen=True)
class WeiboCreatorLink:
    creator_id: str

    @classmethod
    def parse(cls, value: str) -> "WeiboCreatorLink":
        value = value.strip()
        if re.fullmatch(r"[0-9]{1,20}", value):
            if int(value) > 0:
                return cls(str(int(value)))
            raise ValueError("invalid Weibo creator identifier")
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or parsed.hostname not in ("weibo.com", "www.weibo.com", "m.weibo.cn")
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in (None, 80, 443)
        ):
            raise ValueError("unsupported Weibo creator link")
        parts = parsed.path.strip("/").split("/")
        if len(parts) != 2 or parts[0] != "u" or not re.fullmatch(r"[0-9]{1,20}", parts[1]):
            raise ValueError("expected a numeric Weibo creator UID or profile link")
        if int(parts[1]) <= 0:
            raise ValueError("invalid Weibo creator identifier")
        return cls(str(int(parts[1])))
