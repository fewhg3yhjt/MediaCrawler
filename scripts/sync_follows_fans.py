# -*- coding: utf-8 -*-
"""同步我的关注/粉丝列表到关系库。用法: python scripts/sync_follows_fans.py"""
import asyncio
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "weibo_web"))

import config  # noqa
from tools import utils  # noqa
from weibo_web import db  # noqa
from media_platform.weibo.client import WeiboClient  # noqa


async def main() -> None:
    sys.stdout.reconfigure(encoding="utf-8")
    from playwright.async_api import async_playwright
    from tools.cdp_browser import CDPBrowserManager

    db.init_db()
    task_id = db.add_task("social", "", name="同步关注/粉丝")
    try:
        async with async_playwright() as playwright:
            manager = CDPBrowserManager()
            bc = await manager.launch_and_connect(playwright, headless=True)
            page = await bc.new_page()

            # 1) 拿当前 uid（移动端 config，直接 httpx，不触发页面跳转）
            mobile_cookies, _ = utils.convert_cookies(await bc.cookies(urls=["https://m.weibo.cn"]))
            import httpx
            mheaders = {
                "User-Agent": utils.get_mobile_user_agent(), "Cookie": mobile_cookies,
                "Origin": "https://m.weibo.cn", "Referer": "https://m.weibo.cn",
            }
            resp = httpx.get("https://m.weibo.cn/api/config", headers=mheaders, timeout=20, verify=False)
            uid = str(resp.json().get("data", {}).get("uid") or "")
            print("当前用户 uid:", uid)
            # 写路径自检：插入一个测试用户并立即读回
            db.upsert_user({"user_id": "SYNC_TEST_999", "screen_name": "测试", "avatar": "", "description": "", "location": ""})
            test_back = db.get_user("SYNC_TEST_999")
            print(f"[selfcheck] upsert 后读回: {test_back is not None} | DB users={db.stats()['total']}")

            # 2) 检查桌面会话（WBPSESS+ALF），缺失走 SSO 自动补全
            cookies = await bc.cookies(urls=["https://weibo.com"])
            cd = {c["name"]: c["value"] for c in cookies}
            if not (cd.get("WBPSESS") and cd.get("ALF")):
                await page.goto("https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F")
                await asyncio.sleep(6)
                await page.goto("https://weibo.com", wait_until="domcontentloaded")
                await asyncio.sleep(3)
                cookies = await bc.cookies(urls=["https://weibo.com"])
                cd = {c["name"]: c["value"] for c in cookies}
            client = WeiboClient(proxy=None, headers={}, playwright_page=page, cookie_dict={})
            client.update_pc_cookies(cookies)

            # 3) 直接去自己的主页（稳定页面，供 evaluate 使用）
            await page.goto(f"https://weibo.com/u/{uid}", wait_until="domcontentloaded", timeout=40000)
            await asyncio.sleep(8)

            # 记录同步前的关注/粉丝（用于变化检测）
            prev_following = {u["user_id"] for u in db.list_users("following", "")}
            prev_follower = {u["user_id"] for u in db.list_users("follower", "")}

            async def sync(kind: str):
                n = 0
                pg = 1
                seen_ids = set()
                while True:
                    url = f"https://weibo.com/ajax/friendships/friends?uid={uid}&count=20&page={pg}"
                    if kind == "fans":
                        url += "&relate=fans"
                    js = f"""async () => {{
                        const r = await fetch('{url}', {{credentials:'include'}});
                        if (r.status !== 200) return {{err:'HTTP '+r.status}};
                        const j = await r.json().catch(() => null);
                        if (!j || j.ok !== 1) return {{err:'ok='+(j&&j.ok)}};
                        return j;
                    }}"""
                    try:
                        d = await page.evaluate(js)
                        users = d.get("users") or [] if isinstance(d, dict) else []
                    except Exception as e:
                        print(f"{kind} err: {str(e)[:60]}")
                        break
                    if not users:
                        break
                    for u in users:
                        uid_ = str(u.get("id"))
                        seen_ids.add(uid_)
                        db.upsert_user({
                            "user_id": uid_,
                            "screen_name": u.get("screen_name", ""),
                            "avatar": u.get("profile_image_url", ""),
                            "description": u.get("description", ""),
                            "location": u.get("location", ""),
                        })
                        if kind == "following":
                            db.set_relation(uid_, is_following=True)
                        else:
                            db.set_relation(uid_, is_follower=True)
                        n += 1
                    total = d.get("total_number") or 0
                    next_cursor = d.get("next_cursor") or ""
                    print(f"{kind}: page {pg} 累计 {n}/{total} | distinct_ids={len(seen_ids)}")
                    if n % 60 == 0:
                        print(f"[milestone] DB users={db.stats()['total']}")
                    if not users or (total and n >= total):
                        break
                    pg += 1
                    await asyncio.sleep(1.5)
                return n

            nf = await sync("following")
            nfan = await sync("fans")
            # 关系变化检测：对比同步前后
            cur_following = {u["user_id"] for u in db.list_users("following", "")}
            cur_follower = {u["user_id"] for u in db.list_users("follower", "")}
            new_following = cur_following - prev_following
            unfollowed = prev_following - cur_following
            new_follower = cur_follower - prev_follower
            lost_follower = prev_follower - cur_follower
            for uid_ in new_following:
                nm = db.get_user(uid_) and db.get_user(uid_).get("screen_name") or uid_
                db.record_change("new_following", uid_, nm)
            for uid_ in unfollowed:
                nm = db.get_user(uid_) and db.get_user(uid_).get("screen_name") or uid_
                db.record_change("unfollowed", uid_, nm)
            for uid_ in new_follower:
                nm = db.get_user(uid_) and db.get_user(uid_).get("screen_name") or uid_
                db.record_change("new_follower", uid_, nm)
            for uid_ in lost_follower:
                nm = db.get_user(uid_) and db.get_user(uid_).get("screen_name") or uid_
                db.record_change("lost_follower", uid_, nm)
            print(f"同步完成: 关注 {nf}, 粉丝 {nfan} | 新关注 {len(new_following)} 取关 {len(unfollowed)} 新粉 {len(new_follower)} 掉粉 {len(lost_follower)}")
            print(f"[debug] DB_PATH={db.DB_PATH} users_count={len(db.list_users('all',''))}")
            await manager.cleanup(force=True)
        db.finish_task(task_id, "done", f"关注 {nf}, 粉丝 {nfan}")
    except Exception as e:
        import traceback
        traceback.print_exc()
        db.finish_task(task_id, "failed", str(e)[:200])


if __name__ == "__main__":
    asyncio.run(main())
