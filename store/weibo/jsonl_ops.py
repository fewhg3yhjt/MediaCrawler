# -*- coding: utf-8 -*-
"""微博 jsonl 就地更新工具：加载全部记录、部分字段合并/整体替换、重写受影响文件。

jsonl 是 append-only，任何对已存记录的修改都需"读原文件 → 过滤/替换 → 原子写回"。
只重写包含被修改 note_id 的文件，避免全量重写。

注意：历史数据可能存在跨文件重复的 note_id（旧版本遗留），所有就地更新都作用于
匹配到的全部副本，保证读取端（按 note_id 去重）无论读到哪份都能看到最新状态。
"""

import glob
import json
import os
from typing import Dict, List, Optional, Tuple


def _jsonl_dir() -> str:
    import config
    return (config.SAVE_DATA_PATH or "data") + "/weibo/jsonl"


def _iter_content_files(jsonl_dir: Optional[str] = None):
    base = jsonl_dir or _jsonl_dir()
    for path in sorted(glob.glob(os.path.join(base, "*contents*.jsonl"))):
        if path.endswith(".bak"):
            continue
        yield path


def load_all_content_records(jsonl_dir: Optional[str] = None) -> Tuple[Dict[str, Dict], Dict[str, List[str]]]:
    """加载全部 contents 记录。

    Returns:
        (records_by_id, records_by_file)
        - records_by_id: note_id -> 完整记录（多次出现取最后一次）
        - records_by_file: 文件路径 -> [记录]（保持原行顺序）
    """
    records_by_id: Dict[str, Dict] = {}
    records_by_file: Dict[str, List[Dict]] = {}
    for path in _iter_content_files(jsonl_dir):
        records: List[Dict] = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except Exception:
                        continue
                    if not rec.get("note_id"):
                        continue
                    records.append(rec)
                    records_by_id[str(rec["note_id"])] = rec
        except Exception:
            continue
        if records:
            records_by_file[path] = records
    return records_by_id, records_by_file


def _rewrite_file(path: str, records: List[Dict]) -> int:
    tmp_path = path + ".tmp"
    n = 0
    with open(tmp_path, "w", encoding="utf-8") as out:
        for rec in records:
            out.write(json.dumps(rec, ensure_ascii=False) + "\n")
            n += 1
    os.replace(tmp_path, path)
    return n


def apply_content_updates(updates: Dict[str, Dict], mode: str = "merge",
                          jsonl_dir: Optional[str] = None) -> int:
    """对已存记录做就地更新（作用于全部副本），只重写受影响文件。

    Args:
        updates: note_id -> 字段 dict
        mode: 'merge' 部分字段合并（刷新）| 'replace' 整体替换
    Returns:
        实际被修改的记录条数（含重复副本）
    """
    if not updates:
        return 0
    _, records_by_file = load_all_content_records(jsonl_dir)
    changed = 0
    affected: List[Tuple[str, List[Dict]]] = []
    for path, records in records_by_file.items():
        dirty = False
        for rec in records:
            nid = str(rec.get("note_id") or "")
            if nid not in updates:
                continue
            fields = updates[nid]
            if mode == "replace":
                if rec != fields:
                    rec.clear(); rec.update(fields)
                    changed += 1; dirty = True
            else:
                before = dict(rec)
                rec.update(fields)
                if rec != before:
                    changed += 1; dirty = True
        if dirty:
            affected.append((path, records))
    for path, records in affected:
        _rewrite_file(path, records)
    return changed


def mark_availability(note_ids, availability: str, jsonl_dir: Optional[str] = None) -> int:
    """批量标记 availability（'ok' | 'deleted_on_source'）。返回被标记条数。"""
    updates = {str(n): {"availability": availability} for n in note_ids}
    return apply_content_updates(updates, mode="merge", jsonl_dir=jsonl_dir)
