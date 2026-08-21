# -*- coding: utf-8 -*-
"""迁移：把已存的转发原文独立记录(-rt)回填进转发微博的 rt_* 字段，然后删除独立记录。"""
import glob
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
JSONL_DIR = ROOT / "data" / "weibo" / "jsonl"


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    total_files = 0
    total_removed = 0
    total_backfilled = 0
    for path in glob.glob(str(JSONL_DIR / "*_contents_*.jsonl")):
        lines = []
        rt_index = {}  # note_id -> -rt record
        normal = []
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        normal.append(line)
                        continue
                    st = str(d.get("source_type", ""))
                    if st.endswith("-rt"):
                        rt_index[str(d.get("note_id"))] = d
                    else:
                        normal.append(d)
        except Exception as e:
            print(f"跳过 {path}: {e}")
            continue
        if not rt_index:
            continue
        changed = False
        for i, item in enumerate(normal):
            if not isinstance(item, dict):
                continue
            if item.get("is_retweet") == "1":
                rid = str(item.get("retweeted_note_id", ""))
                rt = rt_index.get(rid)
                if rt:
                    item["rt_nick"] = rt.get("nickname", "")
                    item["rt_content"] = rt.get("content", "")
                    item["rt_time"] = rt.get("create_date_time", "")
                    item["rt_images"] = rt.get("image_urls", "[]")
                    total_backfilled += 1
                    changed = True
        # 写回：普通记录 + 回填后的；丢弃 -rt 记录
        if changed or rt_index:
            with open(path, "w", encoding="utf-8") as f:
                for item in normal:
                    f.write((item if isinstance(item, str) else json.dumps(item, ensure_ascii=False)) + "\n")
            total_files += 1
            total_removed += len(rt_index)
            print(f"迁移 {pathlib.Path(path).name}: 回填 {total_backfilled if changed else 0}，删除 -rt {len(rt_index)}")
    print(f"完成：{total_files} 文件，回填 {total_backfilled}，删除独立原文记录 {total_removed}")


if __name__ == "__main__":
    main()
