# -*- coding: utf-8 -*-
"""批量爬取所有已跟踪用户（顺序执行，单用户单实例）。用法: python scripts/batch_crawl_tracked.py"""
import pathlib
import subprocess
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "weibo_web"))

from weibo_web import db  # noqa


def venv_python() -> str:
    if sys.platform == "win32":
        return str(ROOT / ".venv" / "Scripts" / "python.exe")
    return str(ROOT / ".venv" / "bin" / "python")


def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    db.init_db()
    users = db.list_users("tracked", "")
    if not users:
        print("没有已跟踪用户")
        return
    py = venv_python()
    total = len(users)
    print(f"批量爬取开始：共 {total} 个已跟踪用户")
    for i, u in enumerate(users, 1):
        uid = u["user_id"]
        mode = u["crawl_mode"] if u["crawl_mode"] in ("incremental", "full") else "incremental"
        name = u.get("screen_name") or uid
        print(f"[{i}/{total}] 爬取 {name} ({uid}) mode={mode} ...")
        log = ROOT / "logs" / f"web_track_{uid}.log"
        try:
            proc = subprocess.run(
                [py, "scripts/crawl_user.py", uid, mode],
                cwd=str(ROOT), stdout=open(log, "wb"), stderr=subprocess.STDOUT,
                timeout=3600 * 3,
            )
            print(f"[{i}/{total}] {name} 完成 exit={proc.returncode}")
        except subprocess.TimeoutExpired:
            print(f"[{i}/{total}] {name} 超时，跳过")
        except Exception as e:
            print(f"[{i}/{total}] {name} 失败: {e}")
    print("批量爬取结束")


if __name__ == "__main__":
    main()
