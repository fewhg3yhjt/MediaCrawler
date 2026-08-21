# -*- coding: utf-8 -*-
"""按 id 拉取用户基本信息并写入关系库。用法: python scripts/fetch_user_info.py <user_id>"""
import asyncio
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "weibo_web"))

from tools import utils  # noqa
from weibo_web import db  # noqa


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    user_id = sys.argv[1] if len(sys.argv) > 1 else ""
    if not user_id:
        print("用法: python scripts/fetch_user_info.py <user_id>")
        return
    from playwright.async_api import async_playwright
    from tools.cdp_browser import CDPBrowserManager

    db.init_db()
    try:
        async with async_playwright() as playwright:
            manager = CDPBrowserManager()
            bc = await manager.launch_and_connect(playwright, headless=True)
            page = await bc.new_page()
            await page.goto(f"https://weibo.com/u/{user_id}", wait_until="domcontentloaded", timeout=30000)
            await asyncio.sleep(5)
            cookies = await bc.cookies(urls=["https://weibo.com"])
            cd = {c["name"]: c["value"] for c in cookies}
            if not (cd.get("WBPSESS") and cd.get("ALF")):
                await page.goto("https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F")
                await asyncio.sleep(6)
                await page.goto(f"https://weibo.com/u/{user_id}", wait_until="domcontentloaded")
                await asyncio.sleep(4)
            # 用浏览器 fetch 拉用户资料
            js = f"""async () => {{
                const r = await fetch('https://weibo.com/ajax/profile/info?uid={user_id}', {{credentials:'include'}});
                if (r.status !== 200) return {{err:'HTTP '+r.status}};
                const j = await r.json().catch(() => null);
                if (!j || j.ok !== 1) return {{err:'ok='+(j&&j.ok)}};
                return j.data || {{}};
            }}"""
            try:
                d = await page.evaluate(js)
            except Exception:
                d = {}
            user = (d.get("user") if isinstance(d, dict) else None) or {}
            if user.get("id"):
                db.upsert_user({
                    "user_id": str(user["id"]),
                    "screen_name": user.get("screen_name", ""),
                    "avatar": user.get("profile_image_url", ""),
                    "description": user.get("description", ""),
                    "location": user.get("location", ""),
                })
                if user.get("statuses_count"):
                    db.set_total_posts(str(user["id"]), user.get("statuses_count"))
                print(f"已写入用户: {user.get('screen_name')} ({user['id']}) 全量微博 {user.get('statuses_count')}")
            else:
                print(f"未获取到用户 {user_id} 的信息（可能不存在或被限制）")
            await manager.cleanup(force=True)
    except Exception as e:
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(main())
