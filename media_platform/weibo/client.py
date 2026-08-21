# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform/weibo/client.py
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
# @Time    : 2023/12/23 15:40
# @Desc    : Weibo crawler API request client

import asyncio
import copy
import json
import os
import random
import re
from typing import TYPE_CHECKING, Callable, Dict, List, Optional, Union
from urllib.parse import parse_qs, unquote, urlencode

import httpx
from httpx import Response
from playwright.async_api import BrowserContext, Page
from tools.httpx_util import make_async_client
from tenacity import RetryError, retry, stop_after_attempt, wait_fixed

import config
from proxy.proxy_mixin import ProxyRefreshMixin
from tools import utils

if TYPE_CHECKING:
    from proxy.proxy_ip_pool import ProxyIpPool

from .exception import DataFetchError
from .field import SearchType


class WeiboClient(ProxyRefreshMixin):

    def __init__(
        self,
        timeout=60,  # If media crawling is enabled, Weibo images need a longer timeout
        proxy=None,
        *,
        headers: Dict[str, str],
        playwright_page: Page,
        cookie_dict: Dict[str, str],
        proxy_ip_pool: Optional["ProxyIpPool"] = None,
    ):
        self.proxy = proxy
        self.timeout = timeout
        self.headers = headers
        self._host = "https://m.weibo.cn"
        self.playwright_page = playwright_page
        self.cookie_dict = cookie_dict
        self._image_agent_host = "https://i1.wp.com/"
        self.pc_headers: Optional[Dict[str, str]] = None  # weibo.com 桌面版 ajax 请求头
        # 最近一次创作者时间线翻页是否翻到了末尾（用于源端已删检测）
        self.last_creator_exhausted = False
        # Initialize proxy pool (from ProxyRefreshMixin)
        self.init_proxy_pool(proxy_ip_pool)

    def update_pc_cookies(self, cookies: List) -> None:
        """用 weibo.com 桌面版 cookies 构建 PC ajax 请求头（mymblog 等接口需要）。"""
        cookie_str, _ = utils.convert_cookies(cookies)
        self.pc_headers = {
            "User-Agent": utils.get_user_agent(),
            "Cookie": cookie_str,
            "Referer": "https://weibo.com/u/",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json, text/plain, */*",
        }
        utils.logger.info("[WeiboClient.update_pc_cookies] PC ajax headers ready")

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(3))
    async def pc_get(self, url: str) -> Dict:
        """weibo.com ajax GET（httpx + 桌面 cookies）。ok!=1 或非 200 时抛 DataFetchError。
        用线程跑 httpx，避免网络挂死冻结 asyncio 事件循环。"""
        if not self.pc_headers:
            raise DataFetchError("pc_headers not initialized")
        await self._refresh_proxy_if_expired()

        def _sync_get():
            import httpx
            with httpx.Client(verify=not getattr(config, "DISABLE_SSL_VERIFY", False), timeout=30) as client:
                return client.get(url, headers=self.pc_headers)

        response = await asyncio.wait_for(asyncio.to_thread(_sync_get), timeout=45)
        if response.status_code != 200:
            raise DataFetchError(f"pc_get http {response.status_code}: {response.text[:120]}")
        try:
            data: Dict = response.json()
        except json.decoder.JSONDecodeError:
            raise DataFetchError(f"pc_get non-json: {response.text[:120]}")
        if data.get("ok") != 1:
            raise DataFetchError(data.get("message") or data.get("msg") or "pc_get response error")
        return data.get("data", {})

    async def get_creator_notes_pc(self, creator: str, page: int = 1) -> Dict:
        """PC 版创作者主页微博列表：weibo.com/ajax/statuses/mymblog"""
        url = f"https://weibo.com/ajax/statuses/mymblog?uid={creator}&page={page}&feature=0"
        self.pc_headers["Referer"] = f"https://weibo.com/u/{creator}"
        return await self.pc_get(url)

    async def get_self_favorites_pc(self, uid: str, page: int = 1) -> Dict:
        """当前账号收藏列表：weibo.com/ajax/favorites/all_fav。返回 data{status, total_number}"""
        url = f"https://weibo.com/ajax/favorites/all_fav?uid={uid}&page={page}&with_total=true"
        self.pc_headers["Referer"] = f"https://weibo.com/u/page/fav/{uid}"
        return await self.pc_get(url)

    async def get_self_likes_pc(self, uid: str, page: int = 1) -> Dict:
        """当前账号赞过列表：weibo.com/ajax/statuses/likelist。返回 data{list}（无 total，翻到空为止）"""
        url = f"https://weibo.com/ajax/statuses/likelist?uid={uid}&page={page}&with_total=true"
        self.pc_headers["Referer"] = f"https://weibo.com/u/page/like/{uid}"
        return await self.pc_get(url)

    @retry(stop=stop_after_attempt(5), wait=wait_fixed(3))
    async def request(self, method, url, **kwargs) -> Union[Response, Dict]:
        # Check if proxy is expired before each request
        await self._refresh_proxy_if_expired()

        enable_return_response = kwargs.pop("return_response", False)
        async with make_async_client(proxy=self.proxy) as client:
            response = await client.request(method, url, timeout=self.timeout, **kwargs)

        if enable_return_response:
            return response

        try:
            data: Dict = response.json()
        except json.decoder.JSONDecodeError:
            # issue: #771 Search API returns error 432, retry multiple times + update h5 cookies
            utils.logger.error(f"[WeiboClient.request] request {method}:{url} err code: {response.status_code} res:{response.text}")
            await self.playwright_page.goto(self._host)
            await asyncio.sleep(2)
            await self.update_cookies(browser_context=self.playwright_page.context)
            raise DataFetchError(f"get response code error: {response.status_code}")

        ok_code = data.get("ok")
        if ok_code == 0:  # response error
            utils.logger.error(f"[WeiboClient.request] request {method}:{url} err, res:{data}")
            raise DataFetchError(data.get("msg", "response error"))
        elif ok_code != 1:  # unknown error
            utils.logger.error(f"[WeiboClient.request] request {method}:{url} err, res:{data}")
            raise DataFetchError(data.get("msg", "unknown error"))
        else:  # response right
            return data.get("data", {})

    async def get(self, uri: str, params=None, headers=None, **kwargs) -> Union[Response, Dict]:
        final_uri = uri
        if isinstance(params, dict):
            final_uri = (f"{uri}?"
                         f"{urlencode(params)}")

        if headers is None:
            headers = self.headers
        return await self.request(method="GET", url=f"{self._host}{final_uri}", headers=headers, **kwargs)

    async def post(self, uri: str, data: dict) -> Dict:
        json_str = json.dumps(data, separators=(',', ':'), ensure_ascii=False)
        return await self.request(method="POST", url=f"{self._host}{uri}", data=json_str, headers=self.headers)

    async def pong(self) -> bool:
        """get a note to check if login state is ok"""
        utils.logger.info("[WeiboClient.pong] Begin pong weibo...")
        # /api/config 的 login 字段对已存在的会话经常误报 False，
        # 而 SUB 登录 cookie 存在即代表有可用会话（数据接口实测可正常返回）。
        if self.cookie_dict.get("SUB"):
            utils.logger.info("[WeiboClient.pong] SUB cookie exists, treat as logged in")
            return True
        ping_flag = False
        try:
            uri = "/api/config"
            resp_data: Dict = await self.request(method="GET", url=f"{self._host}{uri}", headers=self.headers)
            if resp_data.get("login"):
                ping_flag = True
            else:
                utils.logger.error(f"[WeiboClient.pong] cookie may be invalid and again login...")
        except Exception as e:
            utils.logger.error(f"[WeiboClient.pong] Pong weibo failed: {e}, and try to login again...")
            ping_flag = False
        return ping_flag

    async def update_cookies(self, browser_context: BrowserContext, urls: Optional[List[str]] = None):
        """
        Update cookies from browser context
        :param browser_context: Browser context
        :param urls: Optional list of URLs to filter cookies (e.g., ["https://m.weibo.cn"])
                     If provided, only cookies for these URLs will be retrieved
        """
        if urls:
            cookies = await browser_context.cookies(urls=urls)
            utils.logger.info(f"[WeiboClient.update_cookies] Updating cookies for specific URLs: {urls}")
        else:
            cookies = await browser_context.cookies()
            utils.logger.info("[WeiboClient.update_cookies] Updating all cookies")

        cookie_str, cookie_dict = utils.convert_cookies(cookies)
        self.headers["Cookie"] = cookie_str
        self.cookie_dict = cookie_dict
        utils.logger.info(f"[WeiboClient.update_cookies] Cookie updated successfully, total: {len(cookie_dict)} cookies")

    async def get_note_by_keyword(
        self,
        keyword: str,
        page: int = 1,
        search_type: SearchType = SearchType.DEFAULT,
    ) -> Dict:
        """
        search note by keyword
        :param keyword: Search keyword for Weibo
        :param page: Pagination parameter - current page number
        :param search_type: Search type, see SearchType enum in weibo/field.py
        :return:
        """
        uri = "/api/container/getIndex"
        containerid = f"100103type={search_type.value}&q={keyword}"
        params = {
            "containerid": containerid,
            "page_type": "searchall",
            "page": page,
        }
        return await self.get(uri, params)

    async def get_note_comments(self, mid_id: str, max_id: int, max_id_type: int = 0) -> Dict:
        """get notes comments
        :param mid_id: Weibo ID
        :param max_id: Pagination parameter ID
        :param max_id_type: Pagination parameter ID type
        :return:
        """
        uri = "/comments/hotflow"
        params = {
            "id": mid_id,
            "mid": mid_id,
            "max_id_type": max_id_type,
        }
        if max_id > 0:
            params.update({"max_id": max_id})
        referer_url = f"https://m.weibo.cn/detail/{mid_id}"
        headers = copy.copy(self.headers)
        headers["Referer"] = referer_url

        return await self.get(uri, params, headers=headers)

    async def get_note_all_comments(
        self,
        note_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        max_count: int = 10,
    ):
        """
        get note all comments include sub comments
        :param note_id:
        :param crawl_interval:
        :param callback:
        :param max_count:
        :return:
        """
        result = []
        is_end = False
        max_id = -1
        max_id_type = 0
        fetch_order = 0
        page_no = 1
        while not is_end and len(result) < max_count:
            comments_res = await self.get_note_comments(note_id, max_id, max_id_type)
            max_id: int = comments_res.get("max_id")
            max_id_type: int = comments_res.get("max_id_type")
            comment_list: List[Dict] = comments_res.get("data", [])
            is_end = max_id == 0
            if len(result) + len(comment_list) > max_count:
                comment_list = comment_list[:max_count - len(result)]
            for comment in comment_list:
                fetch_order += 1
                comment["_fetch_order"] = fetch_order
                comment["_fetch_page"] = page_no
                comment["_is_hot_comment"] = "1" if page_no == 1 else "0"
            if callback:  # If callback function exists, execute it
                await callback(note_id, comment_list)
            await asyncio.sleep(crawl_interval)
            result.extend(comment_list)
            sub_comment_result = await self.get_comments_all_sub_comments(
                note_id,
                comment_list,
                callback,
                start_order=fetch_order,
                page_no=page_no,
            )
            fetch_order += len(sub_comment_result)
            result.extend(sub_comment_result)
            page_no += 1
        return result

    @staticmethod
    async def get_comments_all_sub_comments(
        note_id: str,
        comment_list: List[Dict],
        callback: Optional[Callable] = None,
        start_order: int = 0,
        page_no: int = 1,
    ) -> List[Dict]:
        """
        Get all sub-comments of comments
        Args:
            note_id:
            comment_list:
            callback:

        Returns:

        """
        if not config.ENABLE_GET_SUB_COMMENTS:
            utils.logger.info(f"[WeiboClient.get_comments_all_sub_comments] Crawling sub_comment mode is not enabled")
            return []

        res_sub_comments = []
        fetch_order = start_order
        for comment in comment_list:
            sub_comments = comment.get("comments")
            if sub_comments and isinstance(sub_comments, list):
                for sub_comment in sub_comments:
                    fetch_order += 1
                    sub_comment["_fetch_order"] = fetch_order
                    sub_comment["_fetch_page"] = page_no
                    sub_comment["_is_hot_comment"] = comment.get("_is_hot_comment", "0")
                await callback(note_id, sub_comments)
                res_sub_comments.extend(sub_comments)
        return res_sub_comments

    async def get_note_info_by_id(self, note_id: str) -> Dict:
        """
        Get note details by note ID
        :param note_id:
        :return:
        """
        url = f"{self._host}/detail/{note_id}"

        def _sync_fetch():
            import httpx
            with httpx.Client(verify=not getattr(config, "DISABLE_SSL_VERIFY", False), timeout=30) as client:
                return client.request("GET", url, headers=self.headers)

        try:
            response = await asyncio.wait_for(asyncio.to_thread(_sync_fetch), timeout=45)
        except asyncio.TimeoutError:
            raise DataFetchError(f"get weibo detail timeout: {note_id}")
        if response.status_code != 200:
            raise DataFetchError(f"get weibo detail err: {response.text}")
        match = re.search(r'var \$render_data = (\[.*?\])\[0\]', response.text, re.DOTALL)
        if match:
            render_data_json = match.group(1)
            render_data_dict = json.loads(render_data_json)
            note_detail = render_data_dict[0].get("status")
            note_item = {"mblog": note_detail}
            return note_item
        else:
            utils.logger.info(f"[WeiboClient.get_note_info_by_id] $render_data value not found")
            return dict()

    async def get_note_image(self, image_url: str) -> bytes:
        # 直连 sinaimg 带微博 Referer 绕过防盗链（不用 i1.wp.com 外部代理，大陆不可靠）
        large_url = re.sub(r"\/(mw\d+|orj\d+|thumb\d+|bmiddle|original|small|large)\/", "/large/", image_url)
        headers = copy.copy(self.headers)
        headers["Referer"] = "https://weibo.com/"
        async with make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", large_url, timeout=self.timeout, headers=headers, follow_redirects=True)
                response.raise_for_status()
                return response.content
            except Exception as exc:
                utils.logger.error(f"[WeiboClient.get_note_image] {exc.__class__.__name__}: {str(exc)[:80]}")
                return None

    async def get_note_video(self, media_url: str, referer_url: Optional[str] = None) -> Optional[bytes]:
        headers = copy.copy(self.headers)
        if referer_url:
            headers["Referer"] = referer_url
        async with make_async_client(proxy=self.proxy) as client:
            try:
                response = await client.request("GET", media_url, timeout=self.timeout, headers=headers)
                response.raise_for_status()
                return response.content
            except httpx.HTTPError as exc:
                utils.logger.error(f"[WeiboClient.get_note_video] {exc.__class__.__name__} for {exc.request.url} - {exc}")
                return None

    async def get_creator_container_info(self, creator_id: str) -> Dict:
        """
        Get user's container ID, container information represents the real API request path
            fid_container_id: Container ID for user's Weibo detail API
            lfid_container_id: Container ID for user's Weibo list API
        Args:
            creator_id: User ID

        Returns: Dictionary with container IDs

        """
        response = await self.get(f"/u/{creator_id}", return_response=True)
        m_weibocn_params = response.cookies.get("M_WEIBOCN_PARAMS")
        if not m_weibocn_params:
            raise DataFetchError("get containerid failed")
        m_weibocn_params_dict = parse_qs(unquote(m_weibocn_params))
        return {"fid_container_id": m_weibocn_params_dict.get("fid", [""])[0], "lfid_container_id": m_weibocn_params_dict.get("lfid", [""])[0]}

    async def get_creator_info_by_id(self, creator_id: str) -> Dict:
        """
        Get user details by user ID
        Args:
            creator_id:

        Returns:

        """
        uri = "/api/container/getIndex"
        containerid = f"100505{creator_id}"
        params = {
            "jumpfrom": "weibocom",
            "type": "uid",
            "value": creator_id,
            "containerid":containerid,
        }
        user_res = await self.get(uri, params)
        return user_res

    async def get_notes_by_creator(
        self,
        creator: str,
        container_id: str,
        page: int = 1,
    ) -> Dict:
        """
        Get creator's notes
        Args:
            creator: Creator ID
            container_id: Container ID
            page: Page number (m.weibo.cn API paginates by page since 2025)
        Returns:

        """

        uri = "/api/container/getIndex"
        params = {
            "jumpfrom": "weibocom",
            "type": "uid",
            "value": creator,
            "containerid": container_id,
            "page": str(page),
        }
        return await self.get(uri, params)

    async def get_all_notes_by_creator_id(
        self,
        creator_id: str,
        container_id: str,
        crawl_interval: float = 1.0,
        callback: Optional[Callable] = None,
        stop_note_id: str = "",
        start_page: int = 1,
    ) -> List[Dict]:
        """
        Get all posts published by a specified user, this method will continuously fetch all posts from a user
        Args:
            creator_id: Creator user ID
            container_id: Container ID for the user
            crawl_interval: Interval between requests in seconds; <=0 时使用 config.WEIBO_CREATOR_PAGE_SLEEP_SEC 随机间隔
            callback: Optional callback function to process notes
            stop_note_id: 已存档的最新 note_id，翻页到该边界即停（增量抓取）；为空则全量
            start_page: 起始页码（断点续跑时从该页继续）

        Returns: List of all notes

        """
        result = []
        notes_has_more = True
        page = start_page
        self.last_creator_exhausted = False
        # 已翻过的页累计条数（第1页12条，其余每页10条），用于精确判断是否翻到末尾
        crawler_total_count = (12 + 10 * (start_page - 2)) if start_page > 1 else 0
        while notes_has_more:
            try:
                notes_res = await self.get_notes_by_creator(creator_id, container_id, page)
            except RetryError as retry_exc:
                # request() 重试耗尽后抛 RetryError；若根因是"没有更多内容"则视为正常结束
                last_err = retry_exc.last_attempt.exception()
                err_msg = str(last_err or "")
                if "这里还没有内容" in err_msg or "没有更多" in err_msg or "还没有" in err_msg:
                    self.last_creator_exhausted = True
                    utils.logger.info(f"[WeiboClient.get_all_notes_by_creator] no more content at page {page}: {err_msg}, stop")
                    break
                raise
            if not notes_res:
                utils.logger.error(f"[WeiboClient.get_notes_by_creator] The current creator may have been banned by Weibo, so they cannot access the data.")
                break
            page += 1
            if "cards" not in notes_res:
                utils.logger.info(f"[WeiboClient.get_all_notes_by_creator] No 'notes' key found in response: {notes_res}")
                break

            notes = notes_res["cards"]
            utils.logger.info(f"[WeiboClient.get_all_notes_by_creator] got user_id:{creator_id} notes len : {len(notes)}")
            notes = [note for note in notes if note.get("card_type") == 9]
            if callback:
                await callback(notes)
            if crawl_interval > 0:
                await asyncio.sleep(crawl_interval)
            else:
                sleep_sec = random.uniform(*config.WEIBO_CREATOR_PAGE_SLEEP_SEC)
                await asyncio.sleep(sleep_sec)
            result.extend(notes)
            crawler_total_count += len(notes)
            if stop_note_id:
                note_ids = [str(n.get("mblog", {}).get("id", "")) for n in notes if n.get("mblog", {}).get("id")]
                if note_ids and max(int(i) for i in note_ids) <= int(stop_note_id):
                    notes_has_more = False
                    utils.logger.info(
                        f"[WeiboClient.get_all_notes_by_creator] reached already-archived boundary "
                        f"(latest_saved={stop_note_id}), stop incremental crawl"
                    )
                else:
                    notes_has_more = notes_res.get("cardlistInfo", {}).get("total", 0) > crawler_total_count
                    if not notes_has_more:
                        self.last_creator_exhausted = True
            else:
                notes_has_more = notes_res.get("cardlistInfo", {}).get("total", 0) > crawler_total_count
                if not notes_has_more:
                    self.last_creator_exhausted = True
        return result

    async def get_all_notes_by_creator_pc(
        self,
        creator_id: str,
        callback: Optional[Callable] = None,
        stop_note_id: str = "",
        start_page: int = 1,
        on_session_fail: Optional[Callable] = None,
    ) -> List[Dict]:
        """
        PC 版全量/增量抓取创作者微博（weibo.com/ajax/statuses/mymblog，可翻到 2011 年）。
        列表项直接是 mblog，统一包装成 {"mblog": ...} 以兼容下游存储逻辑。
        Args:
            creator_id: 用户 UID
            callback: 处理每页微博的回调
            stop_note_id: 已存档最新 note_id，翻到该边界即停（增量）；为空则抓到底（全量/续跑）
            start_page: 起始页码（断点续跑）
            on_session_fail: 页面请求持续失败(会话被风控)时调用的恢复回调，恢复后重试当前页

        Returns: List of {"mblog": mblog}
        """
        result: List[Dict] = []
        page = start_page
        stop = False
        self.last_creator_exhausted = False
        while not stop:
            data = None
            for attempt in range(3):
                try:
                    data = await self.get_creator_notes_pc(creator_id, page)
                    break
                except Exception:
                    if attempt == 2:
                        raise
                    utils.logger.warning(
                        f"[WeiboClient.get_all_notes_by_creator_pc] page {page} failed, attempt {attempt + 1}/3"
                    )
                    if on_session_fail:
                        try:
                            await on_session_fail()
                        except Exception as ex:
                            utils.logger.error(f"[WeiboClient.get_all_notes_by_creator_pc] session recover failed: {ex}")
            if data is None:
                break
            notes = data.get("list") or []

            # 空列表可能是临时风控响应，不能直接当"到底"：重试几次，仍空则探更深页确认
            if not notes:
                for r in range(3):
                    await asyncio.sleep(10)
                    try:
                        d2 = await self.get_creator_notes_pc(creator_id, page)
                        if d2.get("list"):
                            notes = d2["list"]
                            break
                    except Exception:
                        continue
                if not notes:
                    try:
                        pd = await self.get_creator_notes_pc(creator_id, page + 100)
                        deeper_has_data = bool(pd.get("list"))
                    except Exception:
                        deeper_has_data = False
                    if deeper_has_data:
                        # 更深页有数据 → 当前空是风控，等待后重试；仍空则跳过本页继续，避免卡死
                        utils.logger.info(
                            f"[WeiboClient.get_all_notes_by_creator_pc] page {page} empty but deeper has data, "
                            f"likely rate-limited; wait 60s and retry"
                        )
                        await asyncio.sleep(60)
                        got = False
                        for r in range(3):
                            try:
                                d3 = await self.get_creator_notes_pc(creator_id, page)
                                if d3.get("list"):
                                    notes = d3["list"]
                                    got = True
                                    break
                            except Exception:
                                continue
                        if not got:
                            utils.logger.error(
                                f"[WeiboClient.get_all_notes_by_creator_pc] page {page} still empty after retry, skip to avoid stall"
                            )
                            page += 1
                            continue
                    else:
                        self.last_creator_exhausted = True
                        utils.logger.info(f"[WeiboClient.get_all_notes_by_creator_pc] no more content at page {page}, stop")
                        break

            wrapped = [{"mblog": n} for n in notes if n.get("id")]
            if callback:
                await callback(wrapped)
            result.extend(wrapped)
            sleep_sec = random.uniform(*config.WEIBO_PC_PAGE_SLEEP_SEC)
            await asyncio.sleep(sleep_sec)
            page += 1
            if stop_note_id:
                ids = [str(n.get("id", "")) for n in notes if n.get("id")]
                if ids and max(int(i) for i in ids) <= int(stop_note_id):
                    utils.logger.info(
                        f"[WeiboClient.get_all_notes_by_creator_pc] reached already-archived boundary "
                        f"(latest_saved={stop_note_id}), stop incremental crawl"
                    )
                    stop = True
        utils.logger.info(f"[WeiboClient.get_all_notes_by_creator_pc] finished, total {len(result)} notes (pages {start_page}~{page - 1})")
        return result

    async def get_current_user_info(self) -> Dict:
        """Get current logged-in user info from mobile config API."""
        uri = "/api/config"
        return await self.request(method="GET", url=f"{self._host}{uri}", headers=self.headers)

    async def get_self_feed(
        self,
        feed_type: str,
        page: int = 1,
        uid: str = "",
    ) -> Dict:
        """
        Try fetching current-account favorite/liked feed.
        These endpoints are private and may change frequently, so this method
        iterates through configured candidates until one returns data.
        """
        if feed_type == "favorite":
            candidates = config.WEIBO_FAVORITE_API_CANDIDATES
        elif feed_type == "liked":
            candidates = config.WEIBO_LIKED_API_CANDIDATES
        else:
            raise DataFetchError(f"unsupported self feed type: {feed_type}")

        discovered_api = self.load_discovered_self_api(feed_type)
        if discovered_api:
            cached_uri = discovered_api.get("uri")
            cached_params = discovered_api.get("params", {})
            if cached_uri:
                candidates = [cached_uri] + candidates

        last_error: Optional[Exception] = None
        for candidate in candidates:
            final_candidate = candidate.format(uid=uid)
            is_cached_candidate = bool(discovered_api and candidate == discovered_api.get("uri"))
            if is_cached_candidate:
                uri = final_candidate
                params = copy.deepcopy(discovered_api.get("params", {}))
                params["page"] = page
            elif "?" in final_candidate:
                uri, query_string = final_candidate.split("?", 1)
                params = dict(parse_qs(query_string))
                params = {k: (v[0] if isinstance(v, list) and v else v) for k, v in params.items()}
                params["page"] = page
            else:
                uri = final_candidate
                params = {"page": page}
            try:
                return await self.get(uri, params=params)
            except Exception as exc:
                last_error = exc
                utils.logger.warning(
                    f"[WeiboClient.get_self_feed] candidate failed, feed_type={feed_type}, candidate={final_candidate}, error={exc}"
                )

        raise DataFetchError(f"get {feed_type} feed failed: {last_error}")

    @staticmethod
    def get_discovered_api_file_path() -> str:
        return os.path.join("cache", "weibo_self_feed_api.json")

    def load_discovered_self_api(self, feed_type: str) -> Optional[Dict]:
        file_path = self.get_discovered_api_file_path()
        if not os.path.exists(file_path):
            return None
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data.get(feed_type)
        except Exception as exc:
            utils.logger.warning(f"[WeiboClient.load_discovered_self_api] load cache failed: {exc}")
            return None

    def save_discovered_self_api(self, feed_type: str, api_info: Dict) -> None:
        file_path = self.get_discovered_api_file_path()
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        payload = {}
        if os.path.exists(file_path):
            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    payload = json.load(f)
            except Exception:
                payload = {}
        payload[feed_type] = api_info
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        utils.logger.info(f"[WeiboClient.save_discovered_self_api] saved discovered api for {feed_type}: {api_info}")
