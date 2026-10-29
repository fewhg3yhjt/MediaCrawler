# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/config/weibo_config.py
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


# Weibo platform configuration

# Search type, the specific enumeration value is in media_platform/weibo/field.py
WEIBO_SEARCH_TYPE = "default"

# Specify Weibo ID list
WEIBO_SPECIFIED_ID_LIST = [
    "4982041758140155",
    # ........................
]

# Specify Weibo user ID list
WEIBO_CREATOR_ID_LIST = [
    "1401527553",  # tombkeeper (于旸 / TK教主)
    # ........................
]

# Maximum number of favorite/liked posts to export for the current account.
# 默认取全部（接口返回 total_number 截止）；需要限量可调小
WEIBO_FAVORITE_MAX_COUNT = 50000
WEIBO_LIKED_MAX_COUNT = 50000

# When favorite/liked API calls fail, enable browser-side request discovery.
ENABLE_WEIBO_SELF_API_DISCOVERY = True
WEIBO_SELF_API_DISCOVERY_TIMEOUT_SEC = 25

# Optional manual navigation pages used during discovery.
# 收藏：桌面版 https://weibo.com/u/page/fav/{uid} 使用 weibo.com/ajax/favorites/all_fav
# 赞过：桌面版 https://weibo.com/u/page/like/{uid}（若存在）
WEIBO_FAVORITE_PAGE_URL = "https://weibo.com/u/page/fav/{uid}"
WEIBO_LIKED_PAGE_URL = "https://weibo.com/u/page/like/{uid}"

# Candidate mobile endpoints for favorite/liked feeds.
# Weibo frequently changes private API paths, so these are tried in order.
WEIBO_FAVORITE_API_CANDIDATES = [
    "/api/container/getIndex?containerid=230283{uid}_-_feed_favorites",
    "/api/container/getIndex?containerid=231093_-_selffav",
]
WEIBO_LIKED_API_CANDIDATES = [
    "/api/container/getIndex?containerid=230283{uid}_-_feed_like",
    "/api/container/getIndex?containerid=100103type=like&q=",
]

# Whether to enable the function of crawling the full text of Weibo. It is enabled by default.
# If turned on, it will increase the probability of being risk controlled, which is equivalent to a keyword search request that will traverse all posts and request the post details again.
ENABLE_WEIBO_FULL_TEXT = True

# 创作者主页翻页间隔（秒），列表请求限速，降低被风控概率
# 传入的 crawl_interval<=0 时使用此范围内的随机值
WEIBO_CREATOR_PAGE_SLEEP_SEC = (1.0, 2.0)

# 是否使用 weibo.com 桌面版 ajax 接口抓取创作者主页（默认开）
# m.weibo.cn 只能取最近约 1970 条；weibo.com/ajax/statuses/mymblog 可翻到 ~1500 页，
# 覆盖 2011 年至今的全量历史。需要 weibo.com 桌面登录态（WBPSESS），缺失会自动扫码。
WEIBO_USE_PC_AJAX = True

# PC ajax 翻页随机间隔（秒）。桌面版更敏感，间隔放宽些
WEIBO_PC_PAGE_SLEEP_SEC = (2.0, 4.0)

# 是否使用 weibo.com 桌面版 ajax 接口抓取创作者主页
WEIBO_PC_PAGE_SIZE = 20  # PC 接口约 20 条/页，用于断点续跑页码估算

# 全量/断点续跑模式：默认 False=增量（翻到最新已存边界即停）
# 设为 True 后：无存档则从第 1 页全量抓；有存档则从"最旧已存位置"继续抓到末尾（中断可续跑）
# 也可用环境变量 WEIBO_FULL_CRAWL=1 临时开启
WEIBO_FULL_CRAWL = False

# 重复爬取已存微博时的处理策略：skip | refresh | full-update
#   skip         已存的直接跳过（默认，行为同旧版）
#   refresh      只刷新热度字段（点赞/评论/转发数、last_modify_ts、last_seen_ts），不覆盖正文/媒体
#   full-update  用本次爬取到的完整记录整体覆盖旧记录
# 也可用环境变量 WEIBO_SAVE_POLICY=refresh 临时开启
WEIBO_SAVE_POLICY = "skip"

# 爬取后自动检测"源端已删/被封"的微博并标记 availability=deleted_on_source
# 仅在本次爬取翻到时间线末尾（无更多内容）时才能可靠判定，否则不标记
# 也可用环境变量 WEIBO_SCAN_DELETED=1 临时开启
WEIBO_SCAN_DELETED = False
