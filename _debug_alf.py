# -*- coding: utf-8 -*-
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright
from tools.cdp_browser import CDPBrowserManager
from tools import utils


async def main() -> None:
    async with async_playwright() as playwright:
        manager = CDPBrowserManager()
        # 与 crawler 完全一致的启动参数
        bc = await manager.launch_and_connect(playwright, headless=False, user_agent=utils.get_mobile_user_agent())
        page = await bc.new_page()

        def ck():
            return sorted(c["name"] for c in __import__("asyncio").get_event_loop().run_until_complete(
                bc.cookies(urls=["https://weibo.com"])))

        # 1) 启动后未导航
        c1 = await bc.cookies(urls=["https://weibo.com"])
        print("launch 后 cookies:", sorted(c["name"] for c in c1), "| ALF:", any(c["name"] == "ALF" for c in c1))
        # 2) goto www.weibo.com
        await page.goto("https://www.weibo.com")
        await asyncio.sleep(3)
        c2 = await bc.cookies(urls=["https://weibo.com"])
        print("goto home 后 cookies:", sorted(c["name"] for c in c2), "| ALF:", any(c["name"] == "ALF" for c in c2))
        # 3) goto profile
        await page.goto("https://weibo.com/u/1401527553")
        await asyncio.sleep(3)
        c3 = await bc.cookies(urls=["https://weibo.com"])
        print("goto profile 后 cookies:", sorted(c["name"] for c in c3), "| ALF:", any(c["name"] == "ALF" for c in c3))
        body = await page.evaluate("() => (document.body.innerText||'').slice(0,60)")
        print("profile 页面:", body[:50].replace(chr(10), " "))
        # 4) 若缺 ALF，试 SSO 重导航刷新
        await page.goto("https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F")
        await asyncio.sleep(6)
        c4 = await bc.cookies(urls=["https://weibo.com"])
        print("SSO 导航后 cookies:", sorted(c["name"] for c in c4), "| ALF:", any(c["name"] == "ALF" for c in c4))
        await page.goto("https://weibo.com/u/1401527553")
        await asyncio.sleep(3)
        body2 = await page.evaluate("() => (document.body.innerText||'').slice(0,60)")
        print("SSO 后 profile 页面:", body2[:50].replace(chr(10), " "))

        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
