# -*- coding: utf-8 -*-
"""爬取指定用户的全部微博（跟踪功能）。用法:
    python scripts/crawl_user.py <user_id> [incremental|full]
"""
import asyncio
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "weibo_web"))

import config  # noqa
from media_platform.weibo.core import WeiboCrawler  # noqa
from weibo_web import db  # noqa


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    user_id = sys.argv[1] if len(sys.argv) > 1 else ""
    mode = sys.argv[2] if len(sys.argv) > 2 else "incremental"
    media = sys.argv[3] if len(sys.argv) > 3 else "0"
    if not user_id:
        print("用法: python scripts/crawl_user.py <user_id> [incremental|full] [media:0|1]")
        return
    if media in ("1", "true", "yes"):
        os.environ["WEIBO_MEDIA"] = "1"
        print("媒体下载：开启（图片+视频）")
    db.init_db()
    u = db.get_user(user_id)
    task_id = db.add_task(f"track_{user_id}", user_id, name=f"爬取 {u['screen_name'] if u else user_id}")
    config.WEIBO_CREATOR_ID_LIST = [user_id]
    config.CRAWLER_TYPE = "creator"  # 复用 creator 抓取流程（PC mymblog 通道）
    config.WEIBO_FULL_CRAWL = (mode == "full")
    os.environ["WEIBO_CRAWL_PREFIX"] = f"user_{user_id}"  # 输出文件前缀 user_{id}_contents_*
    print(f"开始爬取用户 {user_id}，模式={mode}")
    crawler = WeiboCrawler()
    try:
        await crawler.start()
        saved = db_count(user_id)
        db.finish_task(task_id, "done", f"{saved} 条")
        print(f"完成：已存 {saved} 条")
    except Exception as e:
        db.finish_task(task_id, "failed", str(e)[:150])
        raise
    finally:
        await crawler.close()


def db_count(user_id: str) -> int:
    from store import weibo as ws
    return ws.count_collection(f"user_{user_id}")


if __name__ == "__main__":
    asyncio.run(main())
