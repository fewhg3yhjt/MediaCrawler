# -*- coding: utf-8 -*-
"""微博数据导出/导入合并（多机协作）。

CLI 入口: python -m weibo_transfer export / import
包结构（zip）:
  manifest.json    # 格式版本、来源机器、时间、覆盖用户/集合、条数
  contents.jsonl   # 微博记录（含 source_host / source_collection / availability / last_seen_ts）
  creators.jsonl   # 用户资料
  images/          # 图片文件（{pic_id}.jpg）
  videos/          # 视频文件（{note_id}_video.mp4）
"""

import glob
import json
import os
import shutil
import socket
import tempfile
import time
import zipfile
from typing import Dict, List, Optional, Set

import config
from tools import utils

EXPORT_FORMAT_VERSION = 1

# source_priority 策略的集合权重（favorite=用户主动收藏 最优先）
_COLLECTION_RANK = {
    "favorite": 5,
    "creator": 4,
    "liked": 3,
    "search": 1,
}


def _data_base() -> str:
    return (config.SAVE_DATA_PATH or "data") + "/weibo"


def _jsonl_dir() -> str:
    return os.path.join(_data_base(), "jsonl")


def _media_dir(sub: str) -> str:
    return os.path.join(_data_base(), sub)


def _collection_of(record: Dict) -> str:
    return str(record.get("source_collection") or record.get("source_type") or "").strip()


def _rank(col: str) -> int:
    base = col.split("_")[0] if col.startswith("user_") else col
    return _COLLECTION_RANK.get(base, 2)


def _iter_jsonl() -> List:
    """遍历本地所有 jsonl（contents + creators），产出 (文件路径, 记录)。"""
    items = []
    for path in sorted(
        glob.glob(os.path.join(_jsonl_dir(), "*contents*.jsonl"))
        + glob.glob(os.path.join(_jsonl_dir(), "*creators*.jsonl"))
    ):
        if path.endswith(".bak"):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        items.append((path, json.loads(line)))
                    except Exception:
                        continue
        except Exception:
            continue
    return items


def _referenced_media(record: Dict) -> List[str]:
    """该记录引用的本地媒体文件列表。图片按 pic_id，视频按 note_id 前缀匹配。"""
    found: List[str] = []
    note_id = str(record.get("note_id") or "")
    try:
        pids = json.loads(record.get("pic_ids") or "[]")
    except Exception:
        pids = []
    img_dir = _media_dir("images")
    if pids and os.path.isdir(img_dir):
        for pid in pids:
            for ext in ("jpg", "jpeg", "png", "gif", "webp"):
                p = os.path.join(img_dir, f"{pid}.{ext}")
                if os.path.isfile(p):
                    found.append(p)
                    break
    vid_dir = _media_dir("videos")
    if note_id and os.path.isdir(vid_dir):
        prefix = f"{note_id}_"
        for name in sorted(os.listdir(vid_dir)):
            if name.startswith(prefix) and os.path.isfile(os.path.join(vid_dir, name)):
                found.append(os.path.join(vid_dir, name))
    return found


def _completeness(record: Dict) -> int:
    """记录完整性评分：文本/图片/视频/源端可见各 +1。"""
    score = 0
    if str(record.get("content") or "").strip():
        score += 1
    try:
        if json.loads(record.get("image_urls") or "[]"):
            score += 1
    except Exception:
        pass
    if record.get("video_url"):
        score += 1
    if record.get("availability") != "deleted_on_source":
        score += 1
    return score


def _pick_winner(local: Dict, incoming: Dict, policy: str) -> str:
    """重复记录二选一。返回 'local' | 'incoming' | 'both'（keep_both）。"""
    if policy == "keep_both":
        return "both"
    if policy == "freshness":
        li = int(local.get("last_modify_ts") or 0)
        ii = int(incoming.get("last_modify_ts") or 0)
        return "incoming" if ii > li else "local"
    if policy == "source_priority":
        lr = _rank(_collection_of(local))
        ir = _rank(_collection_of(incoming))
        if ir != lr:
            return "incoming" if ir > lr else "local"
    # integrity（默认）及 source_priority 同权时：比完整性，再比新鲜度
    ls, is_ = _completeness(local), _completeness(incoming)
    if is_ != ls:
        return "incoming" if is_ > ls else "local"
    li = int(local.get("last_modify_ts") or 0)
    ii = int(incoming.get("last_modify_ts") or 0)
    return "incoming" if ii > li else "local"


def export(users: Optional[List[str]] = None, collections: Optional[List[str]] = None,
           dest: Optional[str] = None) -> str:
    """导出本地微博数据为 zip 包。按用户/集合过滤；返回包路径。"""
    include_users: Optional[Set[str]] = {str(u) for u in users} if users else None
    include_cols: Optional[Set[str]] = set(collections) if collections else None

    records: List[Dict] = []
    creators: List[Dict] = []
    seen_creators: Set[str] = set()
    for path, rec in _iter_jsonl():
        col = _collection_of(rec)
        if include_cols and col not in include_cols:
            continue
        uid = str(rec.get("user_id") or "")
        if include_users and uid not in include_users:
            continue
        if "contents" in path:
            records.append(rec)
        else:
            if uid and uid not in seen_creators:
                seen_creators.add(uid)
                creators.append(rec)

    media_files: List[str] = []
    for rec in records:
        media_files.extend(_referenced_media(rec))
    media_files = list(dict.fromkeys(media_files))

    tmp = tempfile.mkdtemp(prefix="weibo_export_")
    try:
        manifest = {
            "format_version": EXPORT_FORMAT_VERSION,
            "export_host": socket.gethostname(),
            "export_time": int(time.time()),
            "users": sorted({str(r.get("user_id") or "") for r in records}),
            "collections": sorted({_collection_of(r) for r in records}),
            "record_count": len(records),
            "creator_count": len(creators),
            "media_count": len(media_files),
        }
        with open(os.path.join(tmp, "manifest.json"), "w", encoding="utf-8") as f:
            json.dump(manifest, f, ensure_ascii=False, indent=2)
        with open(os.path.join(tmp, "contents.jsonl"), "w", encoding="utf-8") as f:
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        with open(os.path.join(tmp, "creators.jsonl"), "w", encoding="utf-8") as f:
            for r in creators:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        for sub in ("images", "videos"):
            os.makedirs(os.path.join(tmp, sub), exist_ok=True)
        for mf in media_files:
            sub = os.path.basename(os.path.dirname(mf))
            shutil.copy2(mf, os.path.join(tmp, sub, os.path.basename(mf)))

        if not dest:
            dest = os.path.join(_data_base(), "exports")
        os.makedirs(dest, exist_ok=True)
        zip_path = os.path.join(
            dest,
            f"weibo_export_{socket.gethostname()}_{utils.get_current_date()}_{int(time.time())}.zip",
        )
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for root, _, files in os.walk(tmp):
                for fn in files:
                    full = os.path.join(root, fn)
                    zf.write(full, arcname=os.path.relpath(full, tmp))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return zip_path


def import_(zip_path: str, policy: str = "integrity", dry_run: bool = False) -> Dict:
    """导入导出包并合并去重。

    policy: integrity | freshness | source_priority | keep_both
    dry_run: 只统计报告，不落盘。
    返回统计报告 dict。
    """
    if policy not in ("integrity", "freshness", "source_priority", "keep_both"):
        raise ValueError(f"unknown policy: {policy}")

    report = {
        "policy": policy,
        "total": 0,
        "new": 0,
        "duplicate": 0,
        "kept_incoming": 0,
        "kept_local": 0,
        "kept_both": 0,
        "skipped_no_id": 0,
        "media_imported": 0,
        "media_skipped": 0,
        "creators_imported": 0,
    }

    with zipfile.ZipFile(zip_path) as zf:
        names = set(zf.namelist())
        if "manifest.json" not in names or "contents.jsonl" not in names:
            raise ValueError("不是有效的 weibo 导出包（缺 manifest.json / contents.jsonl）")
        manifest = json.loads(zf.read("manifest.json"))
        incoming = [
            json.loads(line)
            for line in zf.read("contents.jsonl").decode("utf-8").splitlines()
            if line.strip()
        ]
        incoming_creators = []
        if "creators.jsonl" in names:
            incoming_creators = [
                json.loads(line)
                for line in zf.read("creators.jsonl").decode("utf-8").splitlines()
                if line.strip()
            ]

    report["format_version"] = manifest.get("format_version")
    report["source_host"] = manifest.get("export_host")

    # 本地已存记录
    local_by_id: Dict[str, Dict] = {}
    local_file_by_id: Dict[str, str] = {}
    for path, rec in _iter_jsonl():
        if "contents" in path:
            nid = str(rec.get("note_id") or "")
            if nid:
                local_by_id[nid] = rec
                local_file_by_id[nid] = path

    new_records: List[Dict] = []
    replaced_ids: Set[str] = set()
    for rec in incoming:
        nid = str(rec.get("note_id") or "")
        if not nid:
            report["skipped_no_id"] += 1
            continue
        report["total"] += 1
        if nid not in local_by_id:
            new_records.append(rec)
            report["new"] += 1
            continue
        report["duplicate"] += 1
        winner = _pick_winner(local_by_id[nid], rec, policy)
        if winner == "incoming":
            local_by_id[nid] = rec
            replaced_ids.add(nid)
            report["kept_incoming"] += 1
        elif winner == "both":
            new_records.append(rec)
            report["kept_both"] += 1
        else:
            report["kept_local"] += 1

    # 媒体合并：本地缺失的文件一律补上（不随记录胜负而丢）
    media_imported = media_skipped = 0
    with zipfile.ZipFile(zip_path) as zf:
        for name in zf.namelist():
            if name.endswith("/"):
                continue
            if name.startswith("images/") or name.startswith("videos/"):
                sub = os.path.basename(os.path.dirname(name))
                target = os.path.join(_data_base(), sub, os.path.basename(name))
                if os.path.exists(target):
                    media_skipped += 1
                    continue
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with open(target, "wb") as out:
                    out.write(zf.read(name))
                media_imported += 1
    report["media_imported"] = media_imported
    report["media_skipped"] = media_skipped

    # creators 合并：按 user_id 去重
    if incoming_creators:
        local_creator_ids = {
            str(rec.get("user_id") or "")
            for path, rec in _iter_jsonl()
            if "creators" in path and rec.get("user_id")
        }
        new_creators = [c for c in incoming_creators if str(c.get("user_id") or "") not in local_creator_ids]
        report["creators_imported"] = len(new_creators)
        if not dry_run and new_creators:
            today = utils.get_current_date()
            os.makedirs(_jsonl_dir(), exist_ok=True)
            with open(os.path.join(_jsonl_dir(), f"creator_creators_{today}.jsonl"), "a", encoding="utf-8") as f:
                for c in new_creators:
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")

    if dry_run:
        report["dry_run"] = True
        return report

    # 重写包含被替换 note_id 的本地文件（保证旧版本被移除、不重复）
    if replaced_ids:
        order: Dict[str, List[str]] = {}
        for path, rec in _iter_jsonl():
            if "contents" in path:
                nid = str(rec.get("note_id") or "")
                if nid:
                    order.setdefault(path, []).append(nid)
        for path, nids in order.items():
            if not any(n in replaced_ids for n in nids):
                continue
            tmp_path = path + ".import_tmp"
            with open(tmp_path, "w", encoding="utf-8") as out:
                for nid in nids:
                    rec = local_by_id.get(nid)
                    if rec:
                        out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            os.replace(tmp_path, path)

    # 写新增记录（含 keep_both 副本）到对应集合的今日文件
    if new_records:
        today = utils.get_current_date()
        by_col: Dict[str, List[Dict]] = {}
        for rec in new_records:
            by_col.setdefault(_collection_of(rec) or "creator", []).append(rec)
        os.makedirs(_jsonl_dir(), exist_ok=True)
        for col, recs in by_col.items():
            with open(os.path.join(_jsonl_dir(), f"{col}_contents_{today}.jsonl"), "a", encoding="utf-8") as f:
                for rec in recs:
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    report["dry_run"] = False
    return report
