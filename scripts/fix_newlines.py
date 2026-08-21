# -*- coding: utf-8 -*-
"""回填修复：用 raw_text(含<br>)重新生成 content 的换行。对所有 contents jsonl 生效。"""
import glob
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
JSONL_DIR = ROOT / "data" / "weibo" / "jsonl"


def clean_text(html: str) -> str:
    t = re.sub(r"<br\s*/?>", "\n", html)
    t = re.sub(r"<.*?>", "", t)
    return t.replace("\u200b", "").strip()


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    total_files = 0
    total_fixed = 0
    for path in glob.glob(str(JSONL_DIR / "*_contents_*.jsonl")):
        lines = []
        changed = False
        try:
            with open(path, encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except Exception:
                        lines.append(line)
                        continue
                    raw = d.get("raw_text") or ""
                    if raw and "<br" in raw:
                        new_content = clean_text(raw)
                        if new_content != d.get("content", ""):
                            d["content"] = new_content
                            changed = True
                    lines.append(json.dumps(d, ensure_ascii=False))
        except Exception as e:
            print(f"跳过 {path}: {e}")
            continue
        if changed:
            with open(path, "w", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
            total_files += 1
            total_fixed += len(lines)
            print(f"已修复 {pathlib.Path(path).name}")
    print(f"完成：{total_files} 个文件，{total_fixed} 条记录更新了换行")


if __name__ == "__main__":
    import sys
    main()
