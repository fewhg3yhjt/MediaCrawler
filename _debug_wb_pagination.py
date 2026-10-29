# -*- coding: utf-8 -*-
import asyncio
import json

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
        await asyncio.sleep(2)
        cookie_str, cookie_dict = utils.convert_cookies(
            await browser_context.cookies(urls=["https://m.weibo.cn"])
        )
        print("cookies:", len(cookie_dict), file=open("_debug_cookie.txt", "w"))
        print("cookies:", len(cookie_dict))
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

        uid = "1401527553"
        containerid = f"107603{uid}"

        def note_ids(res):
            return [n["mblog"]["id"] for n in res.get("cards", []) if n.get("card_type") == 9]

        base_params = {"jumpfrom": "weibocom", "type": "uid", "value": uid, "containerid": containerid}
        res = await client.get("/api/container/getIndex", base_params)
        print("=== BASE ===")
        print("cardlistInfo:", res.get("cardlistInfo"))
        print("top keys:", list(res.keys()))
        ids0 = note_ids(res)
        print("note count:", len(ids0))
        print("ids:", ids0)

        variants = {
            "page=2": dict(base_params, page="2"),
            "since_id=1": dict(base_params, since_id="1"),
            "since_id=0": dict(base_params, since_id="0"),
            "page=2+since_id": dict(base_params, page="2", since_id="0"),
            "no type": {"jumpfrom": "weibocom", "value": uid, "containerid": containerid, "page": "2"},
            "uid param page=2": {"uid": uid, "containerid": containerid, "page": "2"},
        }
        for name, params in variants.items():
            try:
                r = await client.get("/api/container/getIndex", params)
                ids = note_ids(r)
                print(f"=== {name} ===")
                print("cardlistInfo:", r.get("cardlistInfo"))
                print("note count:", len(ids), "ids:", ids[:5])
            except Exception as e:
                print(f"=== {name} ERROR: {e} ===")

        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
