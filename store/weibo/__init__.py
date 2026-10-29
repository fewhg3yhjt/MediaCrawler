# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/store/weibo/__init__.py
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

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2024/1/14 21:34
# @Desc    :

import re
import json
import glob
import os
import socket
from typing import Dict, List, Optional, Tuple

import config
from var import crawler_type_var, source_keyword_var

from . import jsonl_ops
from .weibo_store_media import *
from ._store_impl import *


def get_archive_stats() -> Dict:
    """返回已有存档统计: {count: 去重条数, oldest: 最旧note_id, latest: 最新note_id}。"""
    if config.SAVE_DATA_PATH:
        base = os.path.join(config.SAVE_DATA_PATH, "weibo", "jsonl")
    else:
        base = os.path.join("data", "weibo", "jsonl")
    ids: List[int] = []
    if os.path.isdir(base):
        for path in glob.glob(os.path.join(base, "creator_contents_*.jsonl")):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ids.append(int(json.loads(line).get("note_id") or 0))
                        except Exception:
                            continue
            except Exception:
                continue
    if not ids:
        return {"count": 0, "oldest": "", "latest": ""}
    unique_ids = sorted(set(ids))
    return {"count": len(unique_ids), "oldest": str(unique_ids[0]), "latest": str(unique_ids[-1])}


def get_latest_note_id() -> str:
    """读取已有 jsonl 存档，返回最新一条已保存的 note_id（用于增量抓取：翻到该边界即停）。"""
    return get_archive_stats()["latest"]


def count_collection(prefix: str) -> int:
    """统计指定集合（creator/favorite/liked）已保存的去重条数。"""
    if config.SAVE_DATA_PATH:
        base = os.path.join(config.SAVE_DATA_PATH, "weibo", "jsonl")
    else:
        base = os.path.join("data", "weibo", "jsonl")
    ids: set = set()
    if os.path.isdir(base):
        for path in glob.glob(os.path.join(base, f"{prefix}_contents_*.jsonl")):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            ids.add(str(json.loads(line).get("note_id")))
                        except Exception:
                            continue
            except Exception:
                continue
    return len(ids)


def latest_note_id(prefix: str) -> str:
    """指定集合（按文件前缀）最新一条已保存 note_id，用于单用户增量边界。"""
    if config.SAVE_DATA_PATH:
        base = os.path.join(config.SAVE_DATA_PATH, "weibo", "jsonl")
    else:
        base = os.path.join("data", "weibo", "jsonl")
    latest = 0
    if os.path.isdir(base):
        for path in glob.glob(os.path.join(base, f"{prefix}_contents_*.jsonl")):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            nid = int(json.loads(line).get("note_id") or 0)
                            if nid > latest:
                                latest = nid
                        except Exception:
                            continue
            except Exception:
                continue
    return str(latest) if latest else ""


_saved_ids_cache: Optional[set] = None


def get_saved_note_ids() -> set:
    """读取所有 contents jsonl 中已保存的 note_id 集合（带缓存），用于断点续抓跳过已存。"""
    global _saved_ids_cache
    if _saved_ids_cache is not None:
        return _saved_ids_cache
    if config.SAVE_DATA_PATH:
        base = os.path.join(config.SAVE_DATA_PATH, "weibo", "jsonl")
    else:
        base = os.path.join("data", "weibo", "jsonl")
    ids: set = set()
    if os.path.isdir(base):
        for path in glob.glob(os.path.join(base, "*contents*.jsonl")):
            try:
                with open(path, encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            # 统一转 str，避免 int/str 类型不一致导致去重误判
                            ids.add(str(json.loads(line).get("note_id")))
                        except Exception:
                            continue
            except Exception:
                continue
    _saved_ids_cache = ids
    return ids


def get_save_policy() -> str:
    """当前重复爬取策略：skip | refresh | full-update（环境变量优先）。"""
    policy = os.environ.get("WEIBO_SAVE_POLICY", "") or config.WEIBO_SAVE_POLICY
    return policy if policy in ("skip", "refresh", "full-update") else "skip"


def is_scan_deleted_enabled() -> bool:
    return bool(
        config.WEIBO_SCAN_DELETED
        or os.environ.get("WEIBO_SCAN_DELETED", "").lower() in ("1", "true", "yes", "y")
    )


# ---- 重复爬取策略的缓冲更新（refresh/full-update） ----
# jsonl 是 append-only，就地更新需重写受影响文件；为避免每条已存记录都重写一次，
# store_content 先把更新缓冲到模块级 dict，爬取一轮结束后由 flush_content_updates() 一次性落盘。
_pending_content_updates: Dict[str, Tuple[str, Dict]] = {}  # note_id -> (mode, fields)


def buffer_content_update(note_id: str, mode: str, fields: Dict) -> None:
    _pending_content_updates[str(note_id)] = (mode, fields)


async def flush_content_updates() -> int:
    """把本轮缓冲的已存记录更新一次性落盘（重写受影响文件）。返回更新条数。"""
    global _saved_ids_cache
    if not _pending_content_updates:
        return 0
    updates = dict(_pending_content_updates)
    _pending_content_updates.clear()
    merge_fields: Dict[str, Dict] = {}
    replace_records: Dict[str, Dict] = {}
    for nid, (mode, fields) in updates.items():
        (replace_records if mode == "replace" else merge_fields)[nid] = fields
    changed = jsonl_ops.apply_content_updates(merge_fields, mode="merge")
    changed += jsonl_ops.apply_content_updates(replace_records, mode="replace")
    _saved_ids_cache = None  # 记录已变化，作废旧缓存
    if changed:
        utils.logger.info(f"[store.weibo.flush_content_updates] applied {changed} in-place updates")
    return changed


def mark_availability(note_ids, availability: str) -> int:
    """批量标记 availability（'ok' | 'deleted_on_source'）。返回被标记条数。"""
    global _saved_ids_cache
    n = jsonl_ops.mark_availability(note_ids, availability)
    if n:
        _saved_ids_cache = None
    return n


def count_availability(status: str = "deleted_on_source") -> int:
    """统计指定 availability 状态的记录总数（跨所有集合）。"""
    records_by_id, _ = jsonl_ops.load_all_content_records()
    return sum(1 for rec in records_by_id.values() if rec.get("availability") == status)


def _build_weibo_original_url(mblog: Dict) -> str:
    bid = mblog.get("bid", "") or mblog.get("mblogid", "")
    user_info: Dict = mblog.get("user") or {}
    user_id = user_info.get("id", "")
    if bid and user_id:
        return f"https://weibo.com/{user_id}/{bid}"
    note_id = mblog.get("id", "")
    if note_id:
        return f"https://m.weibo.cn/detail/{note_id}"
    return ""


def _rt_field(rt: Dict, *keys) -> str:
    """嵌套取值（如 rt.user.screen_name）。"""
    v = rt
    for k in keys:
        v = (v or {}).get(k) if isinstance(v, dict) else None
    return str(v or "")


def _clean_rt_content(text: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", text or "")
    t = re.sub(r"<.*?>", "", t)
    return t.replace("\u200b", "").strip()


def _rt_images(rt: Dict) -> str:
    """提取转发原文的图片URL列表（JSON字符串）。"""
    urls = []
    for pic in (rt.get("pics") or []):
        if isinstance(pic, str):
            urls.append(pic)
        elif isinstance(pic, dict):
            u = pic.get("large", {}).get("url") or pic.get("url")
            if u:
                urls.append(u)
    return json.dumps(urls, ensure_ascii=False)


def _extract_media_info(mblog: Dict) -> Dict[str, str]:
    pics = mblog.get("pics") or []
    image_urls: List[str] = []
    pic_ids: List[str] = []
    for pic in pics:
        if isinstance(pic, str):
            image_urls.append(pic)
            pic_ids.append(pic.split("/")[-1].split(".")[0])
        elif isinstance(pic, dict):
            url = pic.get("large", {}).get("url") or pic.get("url") or pic.get("videoSrc")
            if url:
                image_urls.append(url)
            pid = pic.get("pid")
            if pid:
                pic_ids.append(pid)

    media_type = "text"
    video_url = ""
    audio_url = ""
    page_info: Dict = mblog.get("page_info") or {}
    # 转发微博的视频/音频在 retweeted_status.page_info 里，外层只有 infopage 深链 → 回退
    if not page_info.get("urls"):
        rt = mblog.get("retweeted_status") or {}
        rt_pi = rt.get("page_info") or {}
        if rt_pi.get("urls"):
            page_info = rt_pi
    if image_urls:
        media_type = "image"
    if page_info:
        media_type = page_info.get("type") or "video"
        urls = page_info.get("urls") or {}
        video_url = (
            urls.get("mp4_720p_mp4")
            or urls.get("mp4_hd_mp4")
            or urls.get("mp4_sd_mp4")
            or page_info.get("page_url")
            or ""
        )
        audio_url = page_info.get("audio_url") or ""

    return {
        "media_type": media_type,
        "image_urls": json.dumps(image_urls, ensure_ascii=False),
        "video_url": video_url,
        "audio_url": audio_url,
        "pic_ids": json.dumps(pic_ids, ensure_ascii=False),
    }


class WeibostoreFactory:
    STORES = {
        "csv": WeiboCsvStoreImplement,
        "db": WeiboDbStoreImplement,
        "postgres": WeiboDbStoreImplement,
        "json": WeiboJsonStoreImplement,
        "jsonl": WeiboJsonlStoreImplement,
        "sqlite": WeiboSqliteStoreImplement,
        "mongodb": WeiboMongoStoreImplement,
        "excel": WeiboExcelStoreImplement,
    }

    @staticmethod
    def create_store() -> AbstractStore:
        store_class = WeibostoreFactory.STORES.get(config.SAVE_DATA_OPTION)
        if not store_class:
            raise ValueError("[WeibotoreFactory.create_store] Invalid save option only supported csv or db or json or sqlite or mongodb or excel ...")
        return store_class()


async def batch_update_weibo_notes(note_list: List[Dict]):
    """
    Batch update weibo notes
    Args:
        note_list:

    Returns:

    """
    if not note_list:
        return
    for note_item in note_list:
        await update_weibo_note(note_item)


async def update_weibo_note(note_item: Dict):
    """
    Update weibo note
    Args:
        note_item:

    Returns:

    """
    if not note_item:
        return

    mblog: Dict = note_item.get("mblog")
    user_info: Dict = mblog.get("user") or {}
    note_id = mblog.get("id")
    content_text = mblog.get("text", "")
    # 保留换行：<br>/<br/> → \n，再剥其他标签
    clean_text = re.sub(r"<br\s*/?>", "\n", content_text)
    clean_text = re.sub(r"<.*?>", "", clean_text)
    clean_text = clean_text.replace("\u200b", "").strip()
    retweeted_status: Dict = mblog.get("retweeted_status") or {}
    media_info = _extract_media_info(mblog)
    source_type = note_item.get("source_type") or crawler_type_var.get() or "search"
    save_content_item = {
        # Weibo information
        "note_id": note_id,
        "content": clean_text,
        "raw_text": content_text,
        "create_time": utils.rfc2822_to_timestamp(mblog.get("created_at")),
        "create_date_time": str(utils.rfc2822_to_china_datetime(mblog.get("created_at"))),
        "liked_count": str(mblog.get("attitudes_count", 0)),
        "comments_count": str(mblog.get("comments_count", 0)),
        "shared_count": str(mblog.get("reposts_count", 0)),
        "last_modify_ts": utils.get_current_timestamp(),
        # 多机协作/合并去重字段
        "source_host": socket.gethostname(),
        "source_collection": source_type,
        "availability": "ok",
        "last_seen_ts": utils.get_current_timestamp(),
        "note_url": f"https://m.weibo.cn/detail/{note_id}",
        "original_url": _build_weibo_original_url(mblog),
        "ip_location": mblog.get("region_name", "").replace("发布于 ", ""),
        "source_type": source_type,
        "is_retweet": "1" if retweeted_status else "0",
        "retweeted_note_id": str(retweeted_status.get("id", "")),
        "retweeted_original_url": _build_weibo_original_url(retweeted_status) if retweeted_status else "",
        # 转发的原文内嵌（不再存独立记录）
        "rt_nick": _rt_field(retweeted_status, "user", "screen_name"),
        "rt_content": _clean_rt_content(retweeted_status.get("text", "")),
        "rt_time": str(utils.rfc2822_to_china_datetime(retweeted_status.get("created_at"))) if retweeted_status.get("created_at") else "",
        "rt_images": _rt_images(retweeted_status),

        # User information
        "user_id": str(user_info.get("id")),
        "nickname": user_info.get("screen_name", ""),
        "gender": user_info.get("gender", ""),
        "profile_url": user_info.get("profile_url", ""),
        "avatar": user_info.get("profile_image_url", ""),
        "source_keyword": source_keyword_var.get(),
    }
    save_content_item.update(media_info)
    utils.logger.info(f"[store.weibo.update_weibo_note] weibo note id:{note_id}, title:{save_content_item.get('content')[:24]} ...")
    await WeibostoreFactory.create_store().store_content(content_item=save_content_item)


async def batch_update_weibo_note_comments(note_id: str, comments: List[Dict]):
    """
    Batch update weibo note comments
    Args:
        note_id:
        comments:

    Returns:

    """
    if not comments:
        return
    for comment_item in comments:
        await update_weibo_note_comment(note_id, comment_item)


async def update_weibo_note_comment(note_id: str, comment_item: Dict):
    """
    Update weibo note comment
    Args:
        note_id: weibo note id
        comment_item: weibo comment item

    Returns:

    """
    if not comment_item or not note_id:
        return
    comment_id = str(comment_item.get("id"))
    user_info: Dict = comment_item.get("user") or {}
    content_text = comment_item.get("text", "")
    clean_text = re.sub(r"<br\s*/?>", "\n", content_text)
    clean_text = re.sub(r"<.*?>", "", clean_text)
    save_comment_item = {
        "comment_id": comment_id,
        "create_time": utils.rfc2822_to_timestamp(comment_item.get("created_at")),
        "create_date_time": str(utils.rfc2822_to_china_datetime(comment_item.get("created_at"))),
        "note_id": note_id,
        "content": clean_text,
        "raw_text": content_text,
        "sub_comment_count": str(comment_item.get("total_number", 0)),
        "comment_like_count": str(comment_item.get("like_count", 0)),
        "last_modify_ts": utils.get_current_timestamp(),
        "ip_location": comment_item.get("source", "").replace("来自", ""),
        "parent_comment_id": comment_item.get("rootid", ""),
        "comment_order": int(comment_item.get("_fetch_order", 0) or 0),
        "comment_page": int(comment_item.get("_fetch_page", 0) or 0),
        "is_hot_comment": str(comment_item.get("_is_hot_comment", "0")),

        # User information
        "user_id": str(user_info.get("id")),
        "nickname": user_info.get("screen_name", ""),
        "gender": user_info.get("gender", ""),
        "profile_url": user_info.get("profile_url", ""),
        "avatar": user_info.get("profile_image_url", ""),
    }
    utils.logger.info(f"[store.weibo.update_weibo_note_comment] Weibo note comment: {comment_id}, content: {save_comment_item.get('content', '')[:24]} ...")
    await WeibostoreFactory.create_store().store_comment(comment_item=save_comment_item)


async def update_weibo_note_image(picid: str, pic_content, extension_file_name):
    """
    Save weibo note image to local
    Args:
        picid:
        pic_content:
        extension_file_name:

    Returns:

    """
    await WeiboStoreImage().store_image({"pic_id": picid, "pic_content": pic_content, "extension_file_name": extension_file_name})


async def update_weibo_note_video(media_id: str, video_content, extension_file_name):
    """
    Save weibo note video or audio to local
    Args:
        media_id:
        video_content:
        extension_file_name:

    Returns:

    """
    await WeiboStoreVideo().store_video(
        {"media_id": media_id, "video_content": video_content, "extension_file_name": extension_file_name}
    )


async def save_creator(user_id: str, user_info: Dict):
    """
    Save creator information to local
    Args:
        user_id:
        user_info:

    Returns:

    """
    local_db_item = {
        'user_id': user_id,
        'nickname': user_info.get('screen_name'),
        'gender': 'Female' if user_info.get('gender') == "f" else 'Male',
        'avatar': user_info.get('avatar_hd'),
        'desc': user_info.get('description'),
        'ip_location': user_info.get("source", "").replace("来自", ""),
        'follows': user_info.get('follow_count', ''),
        'fans': user_info.get('followers_count', ''),
        'tag_list': '',
        "last_modify_ts": utils.get_current_timestamp(),
    }
    utils.logger.info(f"[store.weibo.save_creator] creator:{local_db_item}")
    await WeibostoreFactory.create_store().store_creator(local_db_item)
