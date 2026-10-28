# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\presentation.py
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

"""Export saved collection results as JSON and a local-only HTML report."""

import html
import json
import os
from pathlib import Path
from urllib.parse import quote
import zipfile
import tempfile


class ImageArchiveWriter:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def write(self, report: dict) -> Path:
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / f'{report["task_id"]}-media.zip'
        manifest = []
        with tempfile.NamedTemporaryFile(dir=self.directory, delete=False) as stream:
            temporary = Path(stream.name)
        try:
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED) as archive:
                for result in report.get("results", []):
                    if result["capability"] not in ("images", "video", "audio"):
                        continue
                    for item in result.get("assets", []):
                        if not item.get("local_path") or item["status"] != "succeeded":
                            continue
                        path = Path(item["local_path"])
                        name = f'{result["capability"]}-{len(manifest) + 1:03d}-{path.name}'
                        archive.write(path, name)
                        manifest.append({"file": name, "capability": result["capability"], "note_id": result["note_id"],
                                         "url": item["asset"]["url"], "bytes": path.stat().st_size})
                archive.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return target


class ImageReportWriter:
    def __init__(self, directory: Path):
        self.directory = directory.resolve()

    def write(self, report: dict) -> dict:
        folder = self.directory / report["task_id"]
        folder.mkdir(parents=True, exist_ok=True)
        cards = []
        text_cards = []
        media_cards = []
        comment_cards = []
        for result in report.get("results", []):
            content = result.get("content")
            if result["capability"] == "text" and content is not None:
                text_cards.append(
                    f'<article><h2>微博 {html.escape(result["note_id"])}</h2>'
                    f'<p>{"正文完整" if content["complete"] else "正文可能不完整"} · '
                    f'{html.escape(result["status"])}</p><pre>{html.escape(content["text"])}</pre>'
                    f'<p>{html.escape(result.get("error_code") or "")}</p></article>'
                )
            if result["capability"] == "comments" and result.get("comment_batch") is not None:
                batch = result["comment_batch"]
                comment_cards.append(
                    f'<article><h2>微博 {html.escape(result["note_id"])}</h2>'
                    f'<p>{len(batch["items"])} 条一级评论 · {batch["pages"]} 页 · '
                    f'{"本次分页结束" if batch["complete"] else "达到数量上限"}</p></article>'
                )
                for item in batch["items"]:
                    comment_cards.append(
                        f'<article><p>#{item["rank"]} {html.escape(item["user_name"] or "微博用户")} · '
                        f'{html.escape(item["created_at"] or "时间未知")}</p>'
                        f'<p>{html.escape(item["text"])}</p><small>赞 {item["like_count"]}</small></article>'
                    )
            if result["capability"] != "images":
                if result["capability"] in ("video", "audio"):
                    for item in result.get("assets", []):
                        path = Path(item["local_path"]) if item.get("local_path") else None
                        link = ""
                        if path is not None and path.is_file():
                            relative = quote(Path(os.path.relpath(path, folder)).as_posix())
                            label = "视频文件" if result["capability"] == "video" else "音频文件"
                            link = f'<a href="{relative}" download>{label}</a>'
                        media_cards.append(
                            f'<article><p>微博 {html.escape(result["note_id"])} · '
                            f'{html.escape(item["status"])}</p>{link}'
                            f'<p>{html.escape(item.get("error_code") or "")}</p></article>'
                        )
                continue
            for item in result.get("assets", []):
                path = Path(item["local_path"]) if item.get("local_path") else None
                preview = ""
                if path is not None and path.is_file():
                    relative = quote(Path(os.path.relpath(path, folder)).as_posix())
                    preview = f'<a href="{relative}"><img loading="lazy" src="{relative}" alt="已下载图片"></a>'
                asset = item["asset"]
                cards.append(
                    f'<article>{preview}<p>微博 {html.escape(result["note_id"])} · '
                    f'{html.escape(item["status"])}</p><p>{html.escape(item.get("error_code") or "")}</p>'
                    f'<a href="{html.escape(asset["url"], quote=True)}" rel="noreferrer">原图地址</a></article>'
                )
        errors = [
            f'<p>微博 {html.escape(r["note_id"])}：{html.escape(r["status"])} '
            f'{html.escape(r.get("error_code") or "")}</p>'
            for r in report.get("results", []) if r["status"] not in ("succeeded", "no_content")
        ]
        document = ('<!doctype html><html lang="zh-CN"><meta charset="utf-8">'
                    '<meta name="viewport" content="width=device-width, initial-scale=1">'
                    '<title>微博采集结果</title><style>'
                    'body{font:16px system-ui;margin:32px;background:#f5f6f8;color:#20242b}'
                    '.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:20px}'
                    'article{padding:16px;background:white;border-radius:12px;overflow-wrap:anywhere}'
                    'img{width:100%;height:300px;object-fit:contain}a{color:#185abc}'
                    'pre{white-space:pre-wrap;overflow-wrap:anywhere}'
                    '</style><h1>微博采集结果</h1>'
                    f'<p>任务：{html.escape(report["task_id"])} · 状态：{html.escape(report["status"])}</p>'
                    + "".join(errors) + '<h2>正文</h2><div class="grid">' + "".join(text_cards)
                    + '</div><h2>图片</h2><div class="grid">' + "".join(cards)
                    + '</div><h2>视频与音频</h2><div class="grid">' + "".join(media_cards)
                    + '</div><h2>热评流一级评论</h2><div class="grid">' + "".join(comment_cards) + '</div></html>')
        manifest = folder / "report.json"
        gallery = folder / "index.html"
        manifest.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        gallery.write_text(document, encoding="utf-8")
        return {"manifest": str(manifest), "gallery": str(gallery)}
