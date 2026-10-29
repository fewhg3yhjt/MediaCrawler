# -*- coding: utf-8 -*-
"""统计 data/weibo/jsonl 下的去重微博条数（供自动化脚本做 before/after 对比）。"""
import glob
import json
import os
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def count_unique(data_base: str = "data") -> int:
    ids = set()
    for path in glob.glob(os.path.join(data_base, "weibo", "jsonl", "creator_contents_*.jsonl")):
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        ids.add(json.loads(line).get("note_id"))
                    except Exception:
                        continue
        except Exception:
            continue
    return len(ids)


if __name__ == "__main__":
    base = sys.argv[1] if len(sys.argv) > 1 else "data"
    print(count_unique(base))
