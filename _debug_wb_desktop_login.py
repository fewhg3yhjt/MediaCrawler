# -*- coding: utf-8 -*-
"""一次性脚本：建立 weibo.com 桌面版登录态（需人工扫码），并验证 mymblog 接口深度。"""
import asyncio
import sys

sys.stdout.reconfigure(encoding="utf-8")

from playwright.async_api import async_playwright
from tools.cdp_browser import CDPBrowserManager
from tools import utils


async def main() -> None:
    async with async_playwright() as playwright:
        manager = CDPBrowserManager()
        # 非 headless，让用户能看到浏览器窗口直接扫码
        browser_context = await manager.launch_and_connect(playwright, headless=False)
        page = await browser_context.new_page()

        # 先看是否已有桌面登录态
        cookies = await browser_context.cookies(urls=["https://weibo.com"])
        cookie_dict = {c["name"]: c["value"] for c in cookies}
        if cookie_dict.get("SUB2"):
            print("已存在 weibo.com 桌面登录态(SUB2)，无需扫码")
        else:
            print("未检测到 weibo.com 桌面登录态，打开登录页，请扫码...")
            await page.goto(
                "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F",
                wait_until="domcontentloaded",
            )
            await asyncio.sleep(5)
            # 截屏保存，用户可直接从截图或浏览器窗口扫码
            await page.screenshot(path="data/weibo_login_screen.png", full_page=False)
            print("已保存登录页截图: data/weibo_login_screen.png（或直接在浏览器窗口扫码）")
            print("等待扫码，最多 150 秒...")

            logged_in = False
            for i in range(150):
                await asyncio.sleep(1)
                cookies = await browser_context.cookies(urls=["https://weibo.com"])
                cookie_dict = {c["name"]: c["value"] for c in cookies}
                if cookie_dict.get("SUB2"):
                    logged_in = True
                    break
                if i % 10 == 0:
                    print(f"  等待中... {i+1}s")
            if not logged_in:
                print("超时未登录，请重试")
                await manager.cleanup(force=True)
                sys.exit(1)
            print(f"登录成功! (等待 {i+1}s)，cookie 数: {len(cookie_dict)}")

        # 验证 mymblog 接口
        print("\n=== 验证 weibo.com/ajax/statuses/mymblog 深度 ===")
        for pg in [1, 200, 1000, 2500]:
            js = f"""async () => {{
                const r = await fetch('https://weibo.com/ajax/statuses/mymblog?uid=1401527553&page={pg}&feature=0', {{credentials:'include'}});
                if (r.status !== 200) return 'page {pg}: HTTP ' + r.status;
                const j = await r.json().catch(() => null);
                if (!j || j.ok !== 1) return 'page {pg}: ok=' + (j && j.ok) + ' msg=' + (j && j.msg);
                const l = (j.data || {{}}).list || [];
                if (!l.length) return 'page {pg}: empty';
                return 'page {pg}: ' + l.length + ' 条 | 最新 ' + l[0].created_at + ' | 最旧 ' + l[l.length-1].created_at;
            }}"""
            try:
                print(await page.evaluate(js))
            except Exception as e:
                print(f"page {pg}: ERR {str(e)[:60]}")

        await manager.cleanup(force=True)


if __name__ == "__main__":
    asyncio.run(main())
