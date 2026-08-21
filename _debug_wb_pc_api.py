# -*- coding: utf-8 -*-
import asyncio
from playwright.async_api import async_playwright
from tools.cdp_browser import CDPBrowserManager
from tools import utils
from tools.httpx_util import make_async_client


async def main() -> None:
    async with async_playwright() as playwright:
        manager = CDPBrowserManager()
        browser_context = await manager.launch_and_connect(playwright, headless=True)
        page = await browser_context.new_page()
        # 先访问桌面版，确保 weibo.com cookie 就绪
        await page.goto("https://weibo.com")
        await asyncio.sleep(3)
        cookie_str, _ = utils.convert_cookies(await browser_context.cookies(urls=["https://weibo.com"]))
        print("weibo.com cookies:", len(cookie_str.split(";")), "含SUB:", "SUB" in cookie_str)

        headers = {
            "User-Agent": utils.get_user_agent(),
            "Cookie": cookie_str,
            "Referer": "https://weibo.com/u/1401527553",
            "Accept": "application/json, text/plain, */*",
        }
        uid = "1401527553"
        async with make_async_client() as client:
            for pg in [1, 100, 500, 1000, 2000, 2500]:
                url = f"https://weibo.com/ajax/statuses/mymblog?uid={uid}&page={pg}&feature=0"
                try:
                    resp = await client.get(url, headers=headers, timeout=30)
                    data = resp.json()
                    if data.get("ok") != 1:
                        print(f"page {pg}: ok={data.get('ok')} msg={str(data.get('msg'))[:40]}")
                        continue
                    d = data.get("data", {})
                    lst = d.get("list", []) or []
                    if not lst:
                        print(f"page {pg}: empty list")
                        continue
                    first = lst[0]
                    last = lst[-1]
                    print(
                        f"page {pg}: {len(lst)} 条 | 最新 {first.get('created_at','')} id={first.get('id')} | "
                        f"最旧 {last.get('created_at','')} id={last.get('id')}"
                    )
                except Exception as e:
                    print(f"page {pg}: ERROR {type(e).__name__} {str(e)[:60]}")

        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
