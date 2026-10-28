# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/scripts\serve_weibo_images.py
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

"""Run the local image collection UI; opening it never reads a login profile."""

import argparse
import math
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from media_platform.weibo.collection.runtime import WebImageExecutor
from media_platform.weibo.collection.web_app import create_image_app
from media_platform.weibo.collection.web_jobs import ImageJobController, WebJobRepository


def main():
    import uvicorn
    parser = argparse.ArgumentParser(description="微博图片采集本地页面")
    parser.add_argument("--port", type=int, default=8767)
    parser.add_argument("--workspace", type=Path, default=ROOT / "data/weibo/collection")
    parser.add_argument("--browser-profile", type=Path, default=ROOT / "browser_data/cdp_wb_user_data_dir")
    parser.add_argument("--account-scope", default="project-weibo")
    parser.add_argument("--interval", type=float, default=2)
    parser.add_argument("--login-timeout", type=float, default=180)
    args = parser.parse_args()
    if (not 1 <= args.port <= 65535 or not math.isfinite(args.interval) or args.interval < 0
            or not math.isfinite(args.login_timeout) or args.login_timeout <= 0):
        parser.error("端口或等待时间无效")
    executor = WebImageExecutor(args.workspace, args.browser_profile, args.account_scope,
                                args.interval, args.login_timeout)
    controller = ImageJobController(WebJobRepository(args.workspace / "web_jobs.sqlite3"), executor)
    print(f"微博图片采集页面：http://127.0.0.1:{args.port}", flush=True)
    uvicorn.run(create_image_app(controller, args.workspace), host="127.0.0.1", port=args.port)


if __name__ == "__main__":
    raise SystemExit(main())
