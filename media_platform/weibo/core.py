# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/core.py
# GitHub: https://github.com/NanmiCoder
# Licensed under NON-COMMERCIAL LEARNING LICENSE 1.1
#

# 声明：本代码仅供学习和研究目的使用。使用者应遵守以下原则：
# 1. 不得用于任何商业用途。
# 2. 使用时应遵守目标平台的使用条款和robots.txt规则。
# 3. 不得进行大规模爬取或对平台造成运营干扰。
# 4. 应合理控制请求频率，避免给目标平台带来不必要的负担。
# 5. 不得用于任何非法或不当的用途。
#
# 详细许可条款请参阅项目根目录下的LICENSE文件。
# 使用本代码即表示您同意遵守上述原则和LICENSE中的所有条款。

# -*- coding: utf-8 -*-
# @Author  : relakkes@gmail.com
# @Time    : 2023/12/23 15:41
# @Desc    : Weibo crawler main workflow code

import asyncio
import os
import random
# import random  # Removed as we now use fixed config.CRAWLER_MAX_SLEEP_SEC intervals
from asyncio import Task
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import parse_qs

from playwright.async_api import (
    BrowserContext,
    BrowserType,
    Page,
    Playwright,
    Response,
    async_playwright,
)

import config
from base.base_crawler import AbstractCrawler
from proxy.proxy_ip_pool import IpInfoModel, create_ip_pool
from store import weibo as weibo_store
from tools import utils
from tools.async_file_writer import AsyncFileWriter
from tools.cdp_browser import CDPBrowserManager
from var import crawler_type_var, source_keyword_var

from .client import WeiboClient
from .exception import DataFetchError
from .field import SearchType
from .help import filter_search_result_card
from .login import WeiboLogin


def extract_retweet_originals(note_list: List[Dict]) -> List[Dict]:
    """把转发微博的原文（retweeted_status）提取为独立记录，供一并保存，避免只存转发外壳。"""
    originals: List[Dict] = []
    seen: set = set()
    for note_item in note_list:
        rt = (note_item.get("mblog") or {}).get("retweeted_status")
        if not rt or not rt.get("id"):
            continue
        rt_id = str(rt.get("id"))
        if rt_id in seen:
            continue
        seen.add(rt_id)
        originals.append({
            "mblog": rt,
            "source_type": str(note_item.get("source_type") or "") + "-rt",
            "_is_retweet_original": True,
        })
    return originals


class WeiboCrawler(AbstractCrawler):
    context_page: Page
    wb_client: WeiboClient
    browser_context: BrowserContext
    cdp_manager: Optional[CDPBrowserManager]

    def __init__(self):
        self.index_url = "https://www.weibo.com"
        self.mobile_index_url = "https://m.weibo.cn"
        self.user_agent = utils.get_user_agent()
        self.mobile_user_agent = utils.get_mobile_user_agent()
        self.cdp_manager = None
        self.ip_proxy_pool = None  # Proxy IP pool for automatic proxy refresh

    async def start(self):
        playwright_proxy_format, httpx_proxy_format = None, None
        if config.ENABLE_IP_PROXY:
            self.ip_proxy_pool = await create_ip_pool(config.IP_PROXY_POOL_COUNT, enable_validate_ip=True)
            ip_proxy_info: IpInfoModel = await self.ip_proxy_pool.get_proxy()
            playwright_proxy_format, httpx_proxy_format = utils.format_proxy_info(ip_proxy_info)

        async with async_playwright() as playwright:
            # Select launch mode based on configuration
            if config.ENABLE_CDP_MODE:
                utils.logger.info("[WeiboCrawler] Launching browser with CDP mode")
                self.browser_context = await self.launch_browser_with_cdp(
                    playwright,
                    playwright_proxy_format,
                    self.mobile_user_agent,
                    headless=config.CDP_HEADLESS,
                )
            else:
                utils.logger.info("[WeiboCrawler] Launching browser with standard mode")
                # Launch a browser context.
                chromium = playwright.chromium
                self.browser_context = await self.launch_browser(chromium, None, self.mobile_user_agent, headless=config.HEADLESS)

                # stealth.min.js is a js script to prevent the website from detecting the crawler.
                await self.browser_context.add_init_script(path="libs/stealth.min.js")


            self.context_page = await self.browser_context.new_page()
            await self.context_page.goto(self.index_url)
            await asyncio.sleep(2)


            # Create a client to interact with the xiaohongshu website.
            self.wb_client = await self.create_weibo_client(httpx_proxy_format)
            if not await self.wb_client.pong():
                login_obj = WeiboLogin(
                    login_type=config.LOGIN_TYPE,
                    login_phone="",  # your phone number
                    browser_context=self.browser_context,
                    context_page=self.context_page,
                    cookie_str=config.COOKIES,
                )
                await login_obj.begin()

                # After successful login, redirect to mobile website and update mobile cookies
                utils.logger.info("[WeiboCrawler.start] redirect weibo mobile homepage and update cookies on mobile platform")
                await self.context_page.goto(self.mobile_index_url)
                await asyncio.sleep(3)
                # Only get mobile cookies to avoid confusion between PC and mobile cookies
                await self.wb_client.update_cookies(
                    browser_context=self.browser_context,
                    urls=[self.mobile_index_url]
                )

            crawler_type_var.set(os.environ.get("WEIBO_CRAWL_PREFIX", "") or config.CRAWLER_TYPE)
            if config.CRAWLER_TYPE == "search":
                # Search for video and retrieve their comment information.
                await self.search()
            elif config.CRAWLER_TYPE == "detail":
                # Get the information and comments of the specified post
                await self.get_specified_notes()
            elif config.CRAWLER_TYPE == "creator":
                if config.WEIBO_USE_PC_AJAX:
                    # PC ajax 通道需要 weibo.com 桌面登录态（缺失则扫码），并准备 PC 请求头
                    await self.ensure_pc_login()
                # Get creator's information and their notes and comments
                await self.get_creators_and_notes()
            elif config.CRAWLER_TYPE == "favorite":
                await self.get_self_notes(feed_type="favorite")
            elif config.CRAWLER_TYPE == "liked":
                await self.get_self_notes(feed_type="liked")
            else:
                pass
            # 把本轮 refresh/full-update 缓冲的已存记录更新一次性落盘
            try:
                await weibo_store.flush_content_updates()
            except Exception as ex:
                utils.logger.error(f"[WeiboCrawler.start] flush_content_updates failed: {str(ex)[:100]}")
            utils.logger.info("[WeiboCrawler.start] Weibo Crawler finished ...")

    async def _extract_pc_cookies(self):
        cookies = await self.browser_context.cookies(urls=["https://weibo.com"])
        cd = {c["name"]: c["value"] for c in cookies}
        return cookies, cd

    async def _pc_session_ok(self) -> bool:
        if not config.WEIBO_CREATOR_ID_LIST:
            return False
        try:
            data = await self.wb_client.get_creator_notes_pc(config.WEIBO_CREATOR_ID_LIST[0], 1)
            return bool(data.get("list"))
        except Exception as ex:
            utils.logger.warning(f"[WeiboCrawler] pc session validation failed: {str(ex)[:100]}")
            return False

    async def _reestablish_pc_session(self) -> bool:
        """通过 SSO 重认证补全 weibo.com 桌面 cookie（已登录会自动跳转重发 cookie，含 ALF），返回是否可用。"""
        sso_url = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F"
        profile_url = f"https://weibo.com/u/{config.WEIBO_CREATOR_ID_LIST[0]}" if config.WEIBO_CREATOR_ID_LIST else "https://weibo.com"
        try:
            await self.context_page.goto(sso_url)
            await asyncio.sleep(6)
            await self.context_page.goto(profile_url)
            await asyncio.sleep(3)
            cookies, cd = await self._extract_pc_cookies()
            self.wb_client.update_pc_cookies(cookies)
            return await self._pc_session_ok()
        except Exception as ex:
            utils.logger.error(f"[WeiboCrawler._reestablish_pc_session] failed: {str(ex)[:100]}")
            return False

    async def ensure_pc_login(self) -> None:
        """确保 weibo.com 桌面登录态完整（WBPSESS+ALF）。缺 cookie 时先走 SSO 重认证自动补全，
        再 httpx 实测校验；校验失败才要求扫码。"""
        profile_url = f"https://weibo.com/u/{config.WEIBO_CREATOR_ID_LIST[0]}" if config.WEIBO_CREATOR_ID_LIST else "https://weibo.com"
        sso_url = "https://passport.weibo.com/sso/signin?entry=miniblog&source=miniblog&url=https%3A%2F%2Fweibo.com%2F"

        # 1) 访问主页，让 weibo.com 下发会话
        await self.context_page.goto(profile_url)
        await asyncio.sleep(3)
        cookies, cd = await self._extract_pc_cookies()
        # 无论会话是否完整都先初始化 PC 请求头（否则校验会因空头误报未登录）
        self.wb_client.update_pc_cookies(cookies)

        # 2) 缺关键 cookie 时走 SSO 重认证补全（已登录会自动跳转重发 cookie，含 ALF）
        if not (cd.get("WBPSESS") and cd.get("ALF")):
            utils.logger.info("[WeiboCrawler.ensure_pc_login] session incomplete, re-auth via SSO ...")
            if await self._reestablish_pc_session():
                utils.logger.info("[WeiboCrawler.ensure_pc_login] SSO re-auth completed")

        # 3) httpx 实测校验
        if await self._pc_session_ok():
            utils.logger.info("[WeiboCrawler.ensure_pc_login] weibo.com desktop session valid")
            return

        # 4) 校验仍失败：尝试多次 SSO 重认证（会话常会自动恢复），都失败才要求扫码
        for _ in range(3):
            if await self._reestablish_pc_session():
                utils.logger.info("[WeiboCrawler.ensure_pc_login] weibo.com desktop session restored")
                return
        utils.logger.info("[WeiboCrawler.ensure_pc_login] weibo.com desktop not logged in, please scan QR in browser")
        await self.context_page.goto(sso_url)
        await asyncio.sleep(5)
        await self.context_page.screenshot(path="data/weibo_login_screen.png")
        for _ in range(150):
            await asyncio.sleep(1)
            if await self._reestablish_pc_session():
                utils.logger.info("[WeiboCrawler.ensure_pc_login] weibo.com desktop login success")
                return
        utils.logger.error("[WeiboCrawler.ensure_pc_login] weibo.com desktop login timeout")
        sys.exit(1)

    async def search(self):
        """
        search weibo note with keywords
        :return:
        """
        utils.logger.info("[WeiboCrawler.search] Begin search weibo keywords")
        weibo_limit_count = 10  # weibo limit page fixed value
        if config.CRAWLER_MAX_NOTES_COUNT < weibo_limit_count:
            config.CRAWLER_MAX_NOTES_COUNT = weibo_limit_count
        start_page = config.START_PAGE

        # Set the search type based on the configuration for weibo
        if config.WEIBO_SEARCH_TYPE == "default":
            search_type = SearchType.DEFAULT
        elif config.WEIBO_SEARCH_TYPE == "real_time":
            search_type = SearchType.REAL_TIME
        elif config.WEIBO_SEARCH_TYPE == "popular":
            search_type = SearchType.POPULAR
        elif config.WEIBO_SEARCH_TYPE == "video":
            search_type = SearchType.VIDEO
        else:
            utils.logger.error(f"[WeiboCrawler.search] Invalid WEIBO_SEARCH_TYPE: {config.WEIBO_SEARCH_TYPE}")
            return

        for keyword in config.KEYWORDS.split(","):
            source_keyword_var.set(keyword)
            utils.logger.info(f"[WeiboCrawler.search] Current search keyword: {keyword}")
            page = 1
            while (page - start_page + 1) * weibo_limit_count <= config.CRAWLER_MAX_NOTES_COUNT:
                if page < start_page:
                    utils.logger.info(f"[WeiboCrawler.search] Skip page: {page}")
                    page += 1
                    continue
                utils.logger.info(f"[WeiboCrawler.search] search weibo keyword: {keyword}, page: {page}")
                search_res = await self.wb_client.get_note_by_keyword(keyword=keyword, page=page, search_type=search_type)
                note_id_list: List[str] = []
                note_list = filter_search_result_card(search_res.get("cards"))
                # If full text fetching is enabled, batch get full text of posts
                note_list = await self.batch_get_notes_full_text(note_list)
                for note_item in note_list:
                    if note_item:
                        mblog: Dict = note_item.get("mblog")
                        if mblog:
                            note_id_list.append(mblog.get("id"))
                            await weibo_store.update_weibo_note(note_item)
                            await self.get_note_images(mblog)
                            await self.get_note_video(mblog)

                page += 1

                # Sleep after page navigation
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[WeiboCrawler.search] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after page {page-1}")

                await self.batch_get_notes_comments(note_id_list)

    async def get_specified_notes(self):
        """
        get specified notes info
        :return:
        """
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list = [self.get_note_info_task(note_id=note_id, semaphore=semaphore) for note_id in config.WEIBO_SPECIFIED_ID_LIST]
        video_details = await asyncio.gather(*task_list)
        for note_item in video_details:
            if note_item:
                await weibo_store.update_weibo_note(note_item)
                await self.get_note_images(note_item.get("mblog", {}))
                await self.get_note_video(note_item.get("mblog", {}))
        await self.batch_get_notes_comments(config.WEIBO_SPECIFIED_ID_LIST)

    async def get_note_info_task(self, note_id: str, semaphore: asyncio.Semaphore) -> Optional[Dict]:
        """
        Get note detail task
        :param note_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                result = await self.wb_client.get_note_info_by_id(note_id)

                # Sleep after fetching note details
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[WeiboCrawler.get_note_info_task] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching note details {note_id}")

                return result
            except DataFetchError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_info_task] Get note detail error: {ex}")
                return None
            except KeyError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_info_task] have not fund note detail note_id:{note_id}, err: {ex}")
                return None

    async def batch_get_notes_comments(self, note_id_list: List[str]):
        """
        batch get notes comments
        :param note_id_list:
        :return:
        """
        if not config.ENABLE_GET_COMMENTS:
            utils.logger.info(f"[WeiboCrawler.batch_get_note_comments] Crawling comment mode is not enabled")
            return

        utils.logger.info(f"[WeiboCrawler.batch_get_notes_comments] note ids:{note_id_list}")
        semaphore = asyncio.Semaphore(config.MAX_CONCURRENCY_NUM)
        task_list: List[Task] = []
        for note_id in note_id_list:
            task = asyncio.create_task(self.get_note_comments(note_id, semaphore), name=note_id)
            task_list.append(task)
        await asyncio.gather(*task_list)

    async def get_note_comments(self, note_id: str, semaphore: asyncio.Semaphore):
        """
        get comment for note id
        :param note_id:
        :param semaphore:
        :return:
        """
        async with semaphore:
            try:
                utils.logger.info(f"[WeiboCrawler.get_note_comments] begin get note_id: {note_id} comments ...")

                # Sleep before fetching comments
                await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
                utils.logger.info(f"[WeiboCrawler.get_note_comments] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds before fetching comments for note {note_id}")

                await self.wb_client.get_note_all_comments(
                    note_id=note_id,
                    crawl_interval=config.CRAWLER_MAX_SLEEP_SEC,  # Use fixed interval instead of random
                    callback=weibo_store.batch_update_weibo_note_comments,
                    max_count=config.CRAWLER_MAX_COMMENTS_COUNT_SINGLENOTES,
                )
            except DataFetchError as ex:
                utils.logger.error(f"[WeiboCrawler.get_note_comments] get note_id: {note_id} comment error: {ex}")
            except Exception as e:
                utils.logger.error(f"[WeiboCrawler.get_note_comments] may be been blocked, err:{e}")

    async def get_note_images(self, mblog: Dict):
        """
        get note images
        :param mblog:
        :return:
        """
        if not (config.ENABLE_GET_MEIDAS or os.environ.get("WEIBO_MEDIA", "").lower() in ("1", "true", "yes")):
            utils.logger.info(f"[WeiboCrawler.get_note_images] Crawling image mode is not enabled")
            return

        pics: List = mblog.get("pics")
        if not pics:
            return
        for pic in pics:
            if isinstance(pic, str):
                url = pic
                pid = url.split("/")[-1].split(".")[0]
            elif isinstance(pic, dict):
                # 优先 large 大图
                url = pic.get("large", {}).get("url") or pic.get("url")
                pid = pic.get("pid", "")
            else:
                continue
            if not url:
                continue
            content = await self.wb_client.get_note_image(url)
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
            utils.logger.info(f"[WeiboCrawler.get_note_images] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching image")
            if content != None:
                extension_file_name = url.split(".")[-1]
                await weibo_store.update_weibo_note_image(pid, content, extension_file_name)

    async def get_note_video(self, mblog: Dict):
        """
        get note video/audio
        :param mblog:
        :return:
        """
        if not (config.ENABLE_GET_MEIDAS or os.environ.get("WEIBO_MEDIA", "").lower() in ("1", "true", "yes")):
            utils.logger.info(f"[WeiboCrawler.get_note_video] Crawling video mode is not enabled")
            return

        page_info: Dict = mblog.get("page_info") or {}
        # 转发视频的 mp4 在 retweeted_status.page_info 里
        if not page_info.get("urls"):
            rt_pi = (mblog.get("retweeted_status") or {}).get("page_info") or {}
            if rt_pi.get("urls"):
                page_info = rt_pi
        if not page_info:
            return

        urls = page_info.get("urls") or {}
        media_candidates = [
            ("video", urls.get("mp4_720p_mp4") or urls.get("mp4_hd_mp4") or urls.get("mp4_sd_mp4")),
            ("audio", page_info.get("audio_url")),
        ]
        note_id = str(mblog.get("id", ""))
        referer_url = f"https://m.weibo.cn/detail/{note_id}" if note_id else self.mobile_index_url
        for media_type, media_url in media_candidates:
            if not media_url:
                continue
            content = await self.wb_client.get_note_video(media_url, referer_url=referer_url)
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
            utils.logger.info(
                f"[WeiboCrawler.get_note_video] Sleeping for {config.CRAWLER_MAX_SLEEP_SEC} seconds after fetching {media_type}"
            )
            if content is None:
                continue
            extension_file_name = media_url.split("?")[0].split(".")[-1] or ("mp3" if media_type == "audio" else "mp4")
            media_id = f"{note_id}_{media_type}" if note_id else f"{page_info.get('page_id', 'unknown')}_{media_type}"
            await weibo_store.update_weibo_note_video(media_id, content, extension_file_name)

    async def get_creators_and_notes(self) -> None:
        """
        Get creator's information and their notes and comments
        Returns:

        """
        utils.logger.info("[WeiboCrawler.get_creators_and_notes] Begin get weibo creators")
        for user_id in config.WEIBO_CREATOR_ID_LIST:
            createor_info_res: Dict = await self.wb_client.get_creator_info_by_id(creator_id=user_id)
            if createor_info_res:
                createor_info: Dict = createor_info_res.get("userInfo", {})
                utils.logger.info(f"[WeiboCrawler.get_creators_and_notes] creator info: {createor_info}")
                if not createor_info:
                    raise DataFetchError("Get creator info error")
                await weibo_store.save_creator(user_id, user_info=createor_info)

                # Create a wrapper callback to get full text before saving data
                async def save_notes_with_full_text(note_list: List[Dict]):
                    # 保存当前页的微博（含全文）；转发原文已内嵌进记录（rt_* 字段），不再存独立记录
                    updated_note_list = await self.batch_get_notes_full_text(note_list)
                    await weibo_store.batch_update_weibo_notes(updated_note_list)

                # 抓取模式：
                #   - 增量（默认）：从第 1 页抓，翻到"最新已存"边界即停
                #   - 全量/断点续跑：WEIBO_FULL_CRAWL=True 或环境变量 WEIBO_FULL_CRAWL=1 时，
                #     无存档从第 1 页全量抓；有存档从"最旧已存位置"往前留缓冲续跑（中断可接着跑）
                full_mode = bool(
                    config.WEIBO_FULL_CRAWL
                    or os.environ.get("WEIBO_FULL_CRAWL", "").lower() in ("1", "true", "yes", "y")
                )
                # 存档统计：按当前输出前缀（creator / user_{id}）计算，保证单用户续传/增量边界正确
                cur_prefix = crawler_type_var.get()
                saved_count = weibo_store.count_collection(cur_prefix)
                stats_latest = weibo_store.latest_note_id(cur_prefix)
                if full_mode:
                    if saved_count == 0:
                        start_page = 1
                    else:
                        # 已存约 N 条，最旧一条的位置≈N（PC 接口约 20 条/页，mobile 约 10 条/页）；
                        # 往前留 5 页缓冲防估计偏差漏抓，重叠部分由去重兜底
                        page_size = config.WEIBO_PC_PAGE_SIZE if config.WEIBO_USE_PC_AJAX else 10
                        start_page = max(1, saved_count // page_size - 5)
                    stop_note_id = ""
                    utils.logger.info(
                        f"[WeiboCrawler.get_creators_and_notes] FULL mode, saved={saved_count}, resume from page {start_page}"
                    )
                else:
                    start_page = 1
                    stop_note_id = stats_latest
                    utils.logger.info(
                        f"[WeiboCrawler.get_creators_and_notes] INCREMENTAL mode, prefix={cur_prefix}, stop at latest_saved={stop_note_id or '(none, first full)'}"
                    )

                if config.WEIBO_USE_PC_AJAX:
                    all_notes_list = await self.wb_client.get_all_notes_by_creator_pc(
                        creator_id=user_id,
                        callback=save_notes_with_full_text,
                        start_page=start_page,
                        stop_note_id=stop_note_id,
                        on_session_fail=self._reestablish_pc_session,
                    )
                else:
                    all_notes_list = await self.wb_client.get_all_notes_by_creator_id(
                        creator_id=user_id,
                        container_id=f"107603{user_id}",
                        crawl_interval=0,
                        callback=save_notes_with_full_text,
                        start_page=start_page,
                        stop_note_id=stop_note_id,
                    )

                note_ids = [note_item.get("mblog", {}).get("id") for note_item in all_notes_list if note_item.get("mblog", {}).get("id")]
                await self.batch_get_notes_comments(note_ids)
                for note_item in all_notes_list:
                    await self.get_note_images(note_item.get("mblog", {}))
                    await self.get_note_video(note_item.get("mblog", {}))

                # 源端已删检测：本次翻页翻到底（无更多内容）且已开启时，把"本应看到却没看到"的标记 deleted_on_source
                if weibo_store.is_scan_deleted_enabled() and getattr(self.wb_client, "last_creator_exhausted", False):
                    seen_ids = {str(n.get("mblog", {}).get("id", "")) for n in all_notes_list if n.get("mblog", {}).get("id")}
                    await self._detect_deleted(user_id, seen_ids)

            else:
                utils.logger.error(f"[WeiboCrawler.get_creators_and_notes] get creator info error, creator_id:{user_id}")

    async def _detect_deleted(self, user_id: str, seen_ids: Set[str]) -> None:
        """半自动源端已删检测。前提：本次爬取翻到了时间线末尾（last_creator_exhausted=True）。
        只处理可达窗口（create_time >= 本次看到的最旧一条）内的记录，避免翻页上限造成的误判；
        把窗口内"未再见到的"标记 deleted_on_source，重新见到的恢复 ok。"""
        try:
            records_by_id, _ = weibo_store.jsonl_ops.load_all_content_records()
        except Exception as ex:
            utils.logger.error(f"[WeiboCrawler._detect_deleted] load failed: {str(ex)[:100]}")
            return
        user_records = {nid: r for nid, r in records_by_id.items() if str(r.get("user_id")) == str(user_id)}
        if not user_records:
            return
        seen = {str(i) for i in seen_ids}
        seen_existing = [user_records[n] for n in seen if n in user_records]
        if not seen_existing:
            utils.logger.info(f"[WeiboCrawler._detect_deleted] user {user_id}: no matching saved records, skip")
            return
        oldest_seen = min(int(r.get("create_time") or 0) for r in seen_existing)
        to_mark = [
            nid for nid, r in user_records.items()
            if nid not in seen
            and int(r.get("create_time") or 0) >= oldest_seen
            and r.get("availability") != "deleted_on_source"
        ]
        if to_mark:
            n = weibo_store.mark_availability(to_mark, "deleted_on_source")
            utils.logger.info(f"[WeiboCrawler._detect_deleted] user {user_id}: marked {n} notes deleted_on_source")
        to_restore = [
            n for n in seen
            if n in user_records and user_records[n].get("availability") == "deleted_on_source"
        ]
        if to_restore:
            n = weibo_store.mark_availability(to_restore, "ok")
            utils.logger.info(f"[WeiboCrawler._detect_deleted] user {user_id}: restored {n} notes to ok")

    async def export_self_pc(self, feed_type: str) -> None:
        """当前账号收藏/点赞全量导出（PC 接口，带会话恢复+限速+断点续传）。
        favorite: all_fav (有 total_number)；liked: likelist (无 total，翻到空为止)。"""
        current_user_info = await self.wb_client.get_current_user_info()
        uid = str(current_user_info.get("uid", "") or "")
        if not uid:
            utils.logger.error(f"[WeiboCrawler.export_self_pc] no uid, abort")
            return
        utils.logger.info(f"[WeiboCrawler.export_self_pc] export {feed_type} for uid={uid}")
        saved_ids = weibo_store.get_saved_note_ids()
        getter = self.wb_client.get_self_favorites_pc if feed_type == "favorite" else self.wb_client.get_self_likes_pc
        max_count = int(
            (config.WEIBO_FAVORITE_MAX_COUNT if feed_type == "favorite" else config.WEIBO_LIKED_MAX_COUNT) or 0
        )
        # 断点续传：从已存位置附近开始，避免每次重过已存区（接口约 19 条/页，留 2 页缓冲）
        saved_col = weibo_store.count_collection(feed_type)
        start_page = max(1, saved_col // 19 - 2) if saved_col else 1
        page = start_page
        exported = 0
        utils.logger.info(f"[WeiboCrawler.export_self_pc] {feed_type} resume from page {page} (已存 {saved_col})")
        while True:
            data = None
            for attempt in range(3):
                try:
                    data = await getter(uid, page)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    utils.logger.warning(f"[WeiboCrawler.export_self_pc] {feed_type} page {page} failed, attempt {attempt + 1}/3")
                    if await self._reestablish_pc_session():
                        continue
            if data is None:
                break
            statuses = data.get("status") or data.get("list") or []
            total_number = data.get("total_number") or 0
            # 空列表可能是风控临时响应：重试几次，仍空则探更深页确认，避免提前结束
            if not statuses:
                for r in range(3):
                    await asyncio.sleep(10)
                    try:
                        d2 = await getter(uid, page)
                        if d2.get("status") or d2.get("list"):
                            statuses = d2.get("status") or d2.get("list") or []
                            break
                    except Exception:
                        continue
                if not statuses:
                    deeper_has_data = False
                    try:
                        pd = await getter(uid, page + 50)
                        deeper_has_data = bool(pd.get("status") or pd.get("list"))
                    except Exception:
                        pass
                    if deeper_has_data:
                        utils.logger.info(f"[WeiboCrawler.export_self_pc] page {page} empty but deeper has data, wait 60s and retry")
                        await asyncio.sleep(60)
                        got = False
                        for r in range(3):
                            try:
                                d3 = await getter(uid, page)
                                if d3.get("status") or d3.get("list"):
                                    statuses = d3.get("status") or d3.get("list") or []
                                    got = True
                                    break
                            except Exception:
                                continue
                        if not got:
                            # 不跳过该页（否则丢数据）：结束本次运行但不打"完成"标记，
                            # 让 supervisor 重启后 resume 重试未存的页面
                            utils.logger.error(
                                f"[WeiboCrawler.export_self_pc] page {page} persistent empty, stop run for resume retry"
                            )
                            break
                    else:
                        utils.logger.info(f"[WeiboCrawler.export_self_pc] no more {feed_type} at page {page}, stop")
                        break
            if not statuses:
                break
            # 断点续传：跳过已保存的；策略非 skip 时已存记录也透传（store 按 refresh/full-update 处理）
            policy = weibo_store.get_save_policy()
            new_items = [
                {"mblog": s, "source_type": feed_type}
                for s in statuses
                if s.get("id") and (str(s.get("id")) not in saved_ids or policy != "skip")
            ]
            if new_items:
                note_items = await self.batch_get_notes_full_text(new_items)
                for note_item in note_items:
                    if not note_item:
                        continue
                    await weibo_store.update_weibo_note(note_item)
                    await self.get_note_images(note_item.get("mblog", {}))
                    await self.get_note_video(note_item.get("mblog", {}))
                    saved_ids.add(note_item.get("mblog", {}).get("id"))
            exported += len(statuses)
            utils.logger.info(
                f"[WeiboCrawler.export_self_pc] {feed_type} page {page}: {len(statuses)} items ({len(new_items)} new), exported {exported}"
                + (f"/{total_number}" if total_number else "")
            )
            if total_number and exported >= total_number:
                break
            if max_count and exported >= max_count:
                utils.logger.info(f"[WeiboCrawler.export_self_pc] reached max_count={max_count}")
                break
            page += 1
            await asyncio.sleep(random.uniform(*config.WEIBO_PC_PAGE_SLEEP_SEC))
        utils.logger.info(f"[WeiboCrawler.export_self_pc] finished {feed_type}, exported {exported}")

    async def get_self_notes(self, feed_type: str) -> None:
        """
        Export current account's favorite or liked weibo feed.
        """
        utils.logger.info(f"[WeiboCrawler.get_self_notes] Begin export self {feed_type} feed")
        source_keyword_var.set(feed_type)

        # 收藏/点赞：优先走 PC ajax 全量通道（需桌面登录态）
        if feed_type in ("favorite", "liked") and config.WEIBO_USE_PC_AJAX:
            await self.ensure_pc_login()
            await self.export_self_pc(feed_type)
            return

        current_user_info = await self.wb_client.get_current_user_info()
        uid = str(current_user_info.get("uid", "") or "")
        max_count = (
            config.WEIBO_FAVORITE_MAX_COUNT if feed_type == "favorite" else config.WEIBO_LIKED_MAX_COUNT
        )
        page = 1
        exported_count = 0
        while exported_count < max_count:
            try:
                feed_res = await self.wb_client.get_self_feed(feed_type=feed_type, page=page, uid=uid)
            except DataFetchError:
                discovered_api = await self.discover_self_feed_api(feed_type=feed_type)
                if not discovered_api:
                    raise
                feed_res = await self.wb_client.get_self_feed(feed_type=feed_type, page=page, uid=uid)
            cards = feed_res.get("cards") or []
            note_list = filter_search_result_card(cards)
            if not note_list:
                utils.logger.info(f"[WeiboCrawler.get_self_notes] Empty card list for page {page}, stop exporting")
                break

            note_list = await self.batch_get_notes_full_text(note_list)
            note_id_list: List[str] = []
            for note_item in note_list:
                if not note_item:
                    continue
                note_item["source_type"] = feed_type
                mblog = note_item.get("mblog") or {}
                note_id = mblog.get("id")
                if note_id:
                    note_id_list.append(note_id)
                await weibo_store.update_weibo_note(note_item)
                await self.get_note_images(mblog)
                await self.get_note_video(mblog)
                exported_count += 1
                if exported_count >= max_count:
                    break

            await self.batch_get_notes_comments(note_id_list)
            page += 1
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)

    async def discover_self_feed_api(self, feed_type: str) -> Optional[Dict]:
        """
        Record actual mobile API requests while the browser is on the target page,
        then cache the most likely self-feed endpoint for favorite/liked.
        """
        if not config.ENABLE_WEIBO_SELF_API_DISCOVERY:
            return None

        timeout_sec = max(int(config.WEIBO_SELF_API_DISCOVERY_TIMEOUT_SEC), 5)
        page_url = (
            config.WEIBO_FAVORITE_PAGE_URL if feed_type == "favorite" else config.WEIBO_LIKED_PAGE_URL
        ) or self.mobile_index_url
        capture_writer = AsyncFileWriter(platform="weibo", crawler_type=f"{crawler_type_var.get()}_{feed_type}")
        captured_candidates: List[Dict] = []
        found_event = asyncio.Event()

        async def handle_response(response: Response):
            response_url = response.url
            if not response_url.startswith(f"{self.mobile_index_url}/api/"):
                return
            if "/api/container/getIndex" not in response_url:
                return

            try:
                payload = await response.json()
            except Exception:
                return

            data = payload.get("data", {}) if isinstance(payload, dict) else {}
            cards = filter_search_result_card(data.get("cards") or [])
            is_valid = len(cards) > 0
            await capture_writer.write_to_jsonl(
                item_type="api_capture",
                item={
                    "feed_type": feed_type,
                    "url": response_url,
                    "status": response.status,
                    "cards_count": len(cards),
                    "page_time": utils.get_current_timestamp(),
                },
            )
            if not is_valid:
                return

            relative_uri = response_url.replace(self.mobile_index_url, "", 1)
            uri, _, query_string = relative_uri.partition("?")
            params = dict(parse_qs(query_string))
            params = {key: (value[0] if isinstance(value, list) and value else value) for key, value in params.items()}
            params.pop("page", None)
            candidate = {
                "uri": uri,
                "params": params,
                "cards_count": len(cards),
                "captured_url": response_url,
            }
            captured_candidates.append(candidate)
            found_event.set()

        self.context_page.on("response", handle_response)
        try:
            await self.context_page.goto(page_url)
            if page_url == self.mobile_index_url:
                utils.logger.info(
                    f"[WeiboCrawler.discover_self_feed_api] Please manually open the {feed_type} page in the browser within {timeout_sec} seconds"
                )
            try:
                await asyncio.wait_for(found_event.wait(), timeout=timeout_sec)
            except asyncio.TimeoutError:
                utils.logger.warning(
                    f"[WeiboCrawler.discover_self_feed_api] Did not capture a valid {feed_type} api within {timeout_sec} seconds"
                )
                return None
        finally:
            self.context_page.remove_listener("response", handle_response)

        if not captured_candidates:
            return None

        best_candidate = max(captured_candidates, key=lambda item: item.get("cards_count", 0))
        self.wb_client.save_discovered_self_api(feed_type, best_candidate)
        utils.logger.info(
            f"[WeiboCrawler.discover_self_feed_api] Captured {feed_type} api: {best_candidate.get('captured_url')}"
        )
        return best_candidate

    async def create_weibo_client(self, httpx_proxy: Optional[str]) -> WeiboClient:
        """Create xhs client"""
        utils.logger.info("[WeiboCrawler.create_weibo_client] Begin create weibo API client ...")
        cookie_str, cookie_dict = utils.convert_cookies(await self.browser_context.cookies(urls=[self.mobile_index_url]))
        weibo_client_obj = WeiboClient(
            proxy=httpx_proxy,
            headers={
                "User-Agent": utils.get_mobile_user_agent(),
                "Cookie": cookie_str,
                "Origin": "https://m.weibo.cn",
                "Referer": "https://m.weibo.cn",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=self.context_page,
            cookie_dict=cookie_dict,
            proxy_ip_pool=self.ip_proxy_pool,  # Pass proxy pool for automatic refresh
        )
        return weibo_client_obj

    async def launch_browser(
        self,
        chromium: BrowserType,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """Launch browser and create browser context"""
        utils.logger.info("[WeiboCrawler.launch_browser] Begin create browser context ...")
        if config.SAVE_LOGIN_STATE:
            user_data_dir = os.path.join(os.getcwd(), "browser_data", config.USER_DATA_DIR % config.PLATFORM)  # type: ignore
            browser_context = await chromium.launch_persistent_context(
                user_data_dir=user_data_dir,
                accept_downloads=True,
                headless=headless,
                proxy=playwright_proxy,  # type: ignore
                viewport={
                    "width": 1920,
                    "height": 1080
                },
                user_agent=user_agent,
                channel="chrome",  # Use system's Chrome stable version
            )
            return browser_context
        else:
            browser = await chromium.launch(headless=headless, proxy=playwright_proxy, channel="chrome")  # type: ignore
            browser_context = await browser.new_context(viewport={"width": 1920, "height": 1080}, user_agent=user_agent)
            return browser_context

    async def launch_browser_with_cdp(
        self,
        playwright: Playwright,
        playwright_proxy: Optional[Dict],
        user_agent: Optional[str],
        headless: bool = True,
    ) -> BrowserContext:
        """
        Launch browser with CDP mode
        """
        try:
            self.cdp_manager = CDPBrowserManager()
            browser_context = await self.cdp_manager.launch_and_connect(
                playwright=playwright,
                playwright_proxy=playwright_proxy,
                user_agent=user_agent,
                headless=headless,
            )

            # Display browser information
            browser_info = await self.cdp_manager.get_browser_info()
            utils.logger.info(f"[WeiboCrawler] CDP browser info: {browser_info}")

            return browser_context

        except Exception as e:
            utils.logger.error(f"[WeiboCrawler] CDP mode startup failed, falling back to standard mode: {e}")
            # Fallback to standard mode
            chromium = playwright.chromium
            return await self.launch_browser(chromium, playwright_proxy, user_agent, headless)

    async def get_note_full_text(self, note_item: Dict) -> Dict:
        """
        Get full text content of a post
        If the post content is truncated (isLongText=True), request the detail API to get complete content
        :param note_item: Post data, contains mblog field
        :return: Updated post data
        """
        if not config.ENABLE_WEIBO_FULL_TEXT:
            return note_item

        mblog = note_item.get("mblog", {})
        if not mblog:
            return note_item

        # Check if it's a long text
        is_long_text = mblog.get("isLongText", False)
        if not is_long_text:
            return note_item

        note_id = mblog.get("id")
        if not note_id:
            return note_item

        try:
            utils.logger.info(f"[WeiboCrawler.get_note_full_text] Fetching full text for note: {note_id}")
            full_note = await self.wb_client.get_note_info_by_id(note_id)
            if full_note and full_note.get("mblog"):
                # Replace original content with complete content
                note_item["mblog"] = full_note["mblog"]
                utils.logger.info(f"[WeiboCrawler.get_note_full_text] Successfully fetched full text for note: {note_id}")

            # Sleep after request to avoid rate limiting
            await asyncio.sleep(config.CRAWLER_MAX_SLEEP_SEC)
        except DataFetchError as ex:
            utils.logger.error(f"[WeiboCrawler.get_note_full_text] Failed to fetch full text for note {note_id}: {ex}")
        except Exception as ex:
            utils.logger.error(f"[WeiboCrawler.get_note_full_text] Unexpected error for note {note_id}: {ex}")

        return note_item

    async def batch_get_notes_full_text(self, note_list: List[Dict]) -> List[Dict]:
        """
        Batch get full text content of posts
        :param note_list: List of posts
        :return: Updated list of posts
        """
        if not config.ENABLE_WEIBO_FULL_TEXT:
            return note_list

        result = []
        for note_item in note_list:
            updated_note = await self.get_note_full_text(note_item)
            result.append(updated_note)
        return result

    async def close(self):
        """Close browser context"""
        # Special handling if using CDP mode
        if self.cdp_manager:
            await self.cdp_manager.cleanup()
            self.cdp_manager = None
        else:
            await self.browser_context.close()
        utils.logger.info("[WeiboCrawler.close] Browser context closed ...")
