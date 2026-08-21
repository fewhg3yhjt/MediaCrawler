# -*- coding: utf-8 -*-
import asyncio
from playwright.async_api import async_playwright
from media_platform.weibo.client import WeiboClient
from tools.cdp_browser import CDPBrowserManager
from tools import utils


async def main() -> None:
    async with async_playwright() as playwright:
        manager = CDPBrowserManager()
        browser_context = await manager.launch_and_connect(playwright, headless=True)
        page = await browser_context.new_page()
        await page.goto("https://m.weibo.cn")
        await asyncio.sleep(3)
        cookie_str, cookie_dict = utils.convert_cookies(
            await browser_context.cookies(urls=["https://m.weibo.cn"])
        )
        client = WeiboClient(
            proxy=None,
            headers={
                "User-Agent": utils.get_mobile_user_agent(),
                "Cookie": cookie_str,
                "Origin": "https://m.weibo.cn",
                "Referer": "https://m.weibo.cn",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=page,
            cookie_dict=cookie_dict,
        )
        print("has SUB:", bool(cookie_dict.get("SUB")), "has MLOGIN:", bool(cookie_dict.get("MLOGIN")))
        # 1) creator info api (used by crawl)
        try:
            r = await client.get("/api/container/getIndex", {"jumpfrom":"weibocom","type":"uid","value":"1401527553","containerid":"1005051401527553"})
            ui = (r.get("userInfo") or {}).get("screen_name")
            print("creator info OK, screen_name:", ui)
        except Exception as e:
            print("creator info FAILED:", e)
        # 2) timeline api (used by crawl)
        try:
            r2 = await client.get("/api/container/getIndex", {"jumpfrom":"weibocom","type":"uid","value":"1401527553","containerid":"1076031401527553","page":"1"})
            notes = [n for n in r2.get("cards", []) if n.get("card_type") == 9]
            print("timeline OK, notes:", len(notes))
        except Exception as e:
            print("timeline FAILED:", e)
        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
