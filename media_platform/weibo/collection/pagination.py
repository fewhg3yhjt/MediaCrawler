# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/media_platform\weibo\collection\pagination.py
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

"""Reusable bounded-overlap and page-drift bookkeeping for numbered sources."""

from copy import deepcopy
from datetime import datetime, timezone

from .models import CollectionError


class PageContinuity:
    """Re-scan a small page window on resume and retain evidence of page drift."""

    def __init__(self, cursor: dict, overlap_pages: int = 2):
        if type(overlap_pages) is not int or overlap_pages < 1:
            raise ValueError("overlap_pages must be a positive integer")
        self.overlap_pages = overlap_pages
        self.frontier = self._integer(cursor, "next_page", 1, 1)
        self.page_high_watermark = self._integer(cursor, "pages_scanned", self.frontier - 1)
        self.empty_pages = self._integer(cursor, "empty_pages", 0)
        self.uid = cursor.get("uid")
        self.overlap_verified = cursor.get("overlap_verified") is True
        self.replaying = self.page_high_watermark > 0 and self.frontier > 1
        self.start_page = max(1, self.frontier - overlap_pages) if self.replaying else self.frontier
        self.replay_end = self.frontier - 1 if self.replaying else 0

        history = cursor.get("page_history")
        if history is None:
            history = {}
        if not isinstance(history, dict):
            raise CollectionError("source_checkpoint_invalid")
        self.history = {}
        for page, ids in history.items():
            if (not isinstance(page, str) or not page.isascii() or not page.isdigit()
                    or int(page) <= 0 or not isinstance(ids, list)
                    or any(not isinstance(note_id, str) for note_id in ids)):
                raise CollectionError("source_checkpoint_invalid")
            self.history[page] = list(ids)

        # Read checkpoints written before page_history was introduced.
        if not self.history and self.page_high_watermark:
            last_ids = cursor.get("last_page_ids")
            if isinstance(last_ids, list) and all(isinstance(item, str) for item in last_ids):
                self.history[str(self.page_high_watermark)] = list(last_ids)

        events = cursor.get("page_drift", [])
        if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
            raise CollectionError("source_checkpoint_invalid")
        self.events = deepcopy(events)
        self.observed_overlap: dict[int, list[str]] = {}
        self.previous_ids = cursor.get("last_page_ids")
        if self.previous_ids is not None and (
            not isinstance(self.previous_ids, list)
            or any(not isinstance(note_id, str) for note_id in self.previous_ids)
        ):
            raise CollectionError("source_checkpoint_invalid")
        if self.replaying:
            # The first fetched page is deliberately an overlap page, so it has no
            # adjacent-page signature from this scan to compare against.
            self.previous_ids = None

    @staticmethod
    def _integer(cursor: dict, name: str, default: int, minimum: int = 0) -> int:
        value = cursor.get(name, default)
        if type(value) is not int or value < minimum:
            raise CollectionError("source_checkpoint_invalid")
        return value

    def is_overlap_page(self, page: int) -> bool:
        return self.replaying and page <= self.replay_end

    def validate_page_signature(self, page: int, ids: list[str], error_code: str) -> None:
        signature = tuple(ids)
        if ids and signature == tuple(self.previous_ids or ()) and self.previous_ids is not None:
            raise CollectionError(error_code)
        self.previous_ids = list(signature)

    def observe_overlap(self, page: int, ids: list[str]) -> None:
        self.observed_overlap[page] = list(ids)

    def finish_overlap(self) -> None:
        observed_ids = set()
        for ids in self.observed_overlap.values():
            observed_ids.update(ids)

        for page, current_ids in sorted(self.observed_overlap.items()):
            previous_ids = self.history.get(str(page))
            if previous_ids is None or previous_ids == current_ids:
                continue
            previous_set, current_set = set(previous_ids), set(current_ids)
            missing = sorted(previous_set - current_set)
            added = sorted(current_set - previous_set)
            recovered = sorted(set(missing) & observed_ids)
            pending = sorted(set(missing) - set(recovered))
            self.events.append({
                "page": page,
                "previous_ids": previous_ids,
                "observed_ids": current_ids,
                "missing_ids": missing,
                "added_ids": added,
                "recovered_ids": recovered,
                "pending_ids": pending,
                "status": "pending" if pending else "recovered" if missing else "changed",
                "observed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })

        self._recover_gaps(observed_ids)
        self.history.update({str(page): ids for page, ids in self.observed_overlap.items()})
        self._trim_history(self.replay_end)
        self.replaying = False
        self.overlap_verified = True

    def observe_forward(self, page: int, ids: list[str]) -> None:
        self._recover_gaps(set(ids))
        self.history[str(page)] = list(ids)
        self.page_high_watermark = max(self.page_high_watermark, page)
        self.previous_ids = list(ids)
        self._trim_history(page)

    def _recover_gaps(self, observed_ids: set[str]) -> None:
        if not observed_ids:
            return
        for event in self.events:
            pending = set(event.get("pending_ids") or event.get("unresolved_ids") or ())
            found = pending & observed_ids
            if not found:
                continue
            recovered = set(event.get("recovered_ids") or ()) | found
            pending -= found
            event["recovered_ids"] = sorted(recovered)
            event["pending_ids"] = sorted(pending)
            event.pop("unresolved_ids", None)
            event["status"] = "pending" if pending else "recovered"

    def _trim_history(self, through_page: int) -> None:
        retained = sorted(
            (int(page), ids) for page, ids in self.history.items()
            if int(page) <= through_page
        )[-self.overlap_pages:]
        self.history = {str(page): ids for page, ids in retained}

    def cursor(self, next_page: int, empty_pages: int, complete: bool = False) -> dict:
        events = deepcopy(self.events)
        if complete:
            for event in events:
                pending = event.get("pending_ids") or []
                if pending:
                    event["status"] = "unresolved"
                    event["unresolved_ids"] = pending
                    event["pending_ids"] = []
        result = {
            "next_page": next_page,
            "pages_scanned": self.page_high_watermark,
            "empty_pages": empty_pages,
            "last_page_ids": list(self.previous_ids or []),
            "page_history": self.history,
            "page_drift": self._retained_events(events),
            "overlap_pages": self.overlap_pages,
            "overlap_verified": self.overlap_verified,
        }
        if self.uid is not None:
            result["uid"] = self.uid
        return result

    @staticmethod
    def _retained_events(events: list[dict]) -> list[dict]:
        active = [event for event in events if event.get("status") in ("pending", "unresolved")]
        resolved = [event for event in events if event.get("status") not in ("pending", "unresolved")]
        remaining_slots = max(0, 100 - len(active))
        return resolved[-remaining_slots:] + active if remaining_slots else active

