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
        print("mobile cookies:", list(cookie_dict.keys()))
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
        try:
            resp = await client.request(method="GET", url="https://m.weibo.cn/api/config", headers=client.headers)
            print("api/config:", resp)
        except Exception as e:
            print("api/config ERROR:", e)
        # also check all cookies
        all_cookies = await browser_context.cookies()
        print("all cookie names:", [c["name"] for c in all_cookies])
        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
