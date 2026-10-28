# -*- coding: utf-8 -*-
# Copyright (c) 2025 relakkes@gmail.com
#
# This file is part of MediaCrawler project.
# Repository: https://github.com/NanmiCoder/MediaCrawler/blob/main/tests\test_weibo_image_web.py
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

"""Local UI/API lifecycle tests, without a browser profile or live network."""

import asyncio
import io
import json
import zipfile
from contextlib import asynccontextmanager
from dataclasses import asdict

import httpx
from PIL import Image

from media_platform.weibo.collection.models import CollectionError, MediaAsset, DownloadedAsset, ProgressEvent
from media_platform.weibo.collection.repository import FileAssetRepository
from media_platform.weibo.collection.web_app import create_image_app
from media_platform.weibo.collection.web_jobs import ImageJobController, ImageWebJob, WebJobRepository


def png():
    stream = io.BytesIO()
    Image.new("RGB", (3, 2), "blue").save(stream, format="PNG")
    return stream.getvalue()


class Executor:
    def __init__(self, workspace):
        self.workspace = workspace
        self.calls = []
        self.mode = "success"
        self.block = asyncio.Event()

    async def execute(self, job, progress, on_qr, login):
        self.calls.append((job.use_browser, login, job.task_id))
        if self.mode == "blocked":
            on_qr(png())
            await self.block.wait()
        if self.mode == "login_timeout":
            on_qr(png())
            raise CollectionError("login_timeout")
        if self.mode == "restricted":
            raise CollectionError("login_required")
        task_id = job.task_id or job.id
        progress(ProgressEvent("task_started", task_id=task_id))
        progress(ProgressEvent("images", completed=1, total=1))
        asset = MediaAsset("a", "123", "123", "https://wx1.sinaimg.cn/large/a.png")
        download = job.request["collect"]["images"].get("download", False)
        path = FileAssetRepository(self.workspace / "images").save("test", asset, DownloadedAsset(png(), "png")) if download else None
        return {"task_id": task_id, "status": "succeeded", "results": [
            {"note_id": "123", "capability": "images", "status": "succeeded", "assets": [
                {"asset": asdict(asset), "status": "succeeded", "local_path": str(path) if path else None, "error_code": None}
            ]}
        ]}


@asynccontextmanager
async def environment(tmp_path, executor=None):
    executor = executor or Executor(tmp_path)
    repository = WebJobRepository(tmp_path / "jobs.sqlite3")
    controller = ImageJobController(repository, executor)
    app = create_image_app(controller, tmp_path)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            yield client, controller, executor, {"X-Collection-Token": app.state.csrf_token}


async def wait(controller):
    await asyncio.wait_for(controller.active, timeout=3)


def test_web_download_history_zip_and_paths(tmp_path):
    async def execute():
        async with environment(tmp_path) as (client, controller, executor, headers):
            home = await client.get("/")
            assert home.status_code == 200 and "__COLLECTION_TOKEN__" not in home.text
            response = await client.post("/api/jobs", headers=headers, json={"url": "https://m.weibo.cn/detail/123"})
            assert response.status_code == 202
            job_id = response.json()["id"]
            await wait(controller)
            state = (await client.get(f"/api/jobs/{job_id}")).json()
            assert state["phase"] == "completed" and state["completed"] == state["total"] == 1
            assert state["images"][0]["downloaded"]
            assert "local_path" not in json.dumps(state)
            assert executor.calls == [(False, False, None)]
            image = await client.get(f"/api/jobs/{job_id}/images/0")
            assert image.content == png()
            download = await client.get(f"/api/jobs/{job_id}/images/0?download=true")
            assert "attachment" in download.headers["content-disposition"]
            packed = await client.get(f"/api/jobs/{job_id}/archive")
            with zipfile.ZipFile(io.BytesIO(packed.content)) as archive:
                assert archive.testzip() is None
                manifest = json.loads(archive.read("manifest.json"))
                assert len(manifest) == 1 and "local_path" not in archive.read("manifest.json").decode()
            assert (await client.get("/api/jobs")).json()[0]["id"] == job_id
            assert (await client.get(f"/api/jobs/{job_id}/images/-1")).status_code == 404
            job = controller.repository.get(job_id)
            job.report["results"][0]["assets"][0]["local_path"] = str(tmp_path / "outside.png")
            controller.repository.save(job)
            assert (await client.get(f"/api/jobs/{job_id}/images/0")).status_code == 404
    asyncio.run(execute())


def test_invalid_inputs_and_missing_token_never_start_work(tmp_path):
    async def execute():
        async with environment(tmp_path) as (client, controller, executor, headers):
            assert (await client.post("/api/jobs", json={"url": "123"})).status_code == 403
            assert (await client.post("/api/jobs", headers=headers, json={"url": "https://evil.test/123"})).status_code == 422
            assert (await client.post("/api/jobs", headers=headers, json={"url": "123", "download": "false"})).status_code == 422
            assert (await client.get("/api/jobs/missing")).status_code == 404
            assert not controller.repository.list() and not executor.calls
    asyncio.run(execute())


def test_metadata_only_has_no_files_or_archive(tmp_path):
    async def execute():
        async with environment(tmp_path) as (client, controller, executor, headers):
            response = await client.post("/api/jobs", headers=headers, json={"url": "123", "download": False})
            job_id = response.json()["id"]
            await wait(controller)
            assert not controller.snapshot(job_id)["images"][0]["downloaded"]
            assert not (tmp_path / "images").exists()
            assert (await client.get(f"/api/jobs/{job_id}/images/0")).status_code == 404
            assert (await client.get(f"/api/jobs/{job_id}/archive")).status_code == 409
    asyncio.run(execute())


def test_qr_timeout_keeps_server_alive_and_requires_login_consent(tmp_path):
    async def execute():
        executor = Executor(tmp_path)
        executor.mode = "restricted"
        async with environment(tmp_path, executor) as (client, controller, _, headers):
            response = await client.post("/api/jobs", headers=headers, json={"url": "123"})
            job_id = response.json()["id"]
            await wait(controller)
            rejected = await client.post(f"/api/jobs/{job_id}/retry", headers=headers, json={"login": True})
            assert rejected.status_code == 400
            assert len(executor.calls) == 1
            executor.mode = "login_timeout"
            response = await client.post(f"/api/jobs/{job_id}/retry", headers=headers, json={"login": True, "consent": True})
            assert response.status_code == 202
            await wait(controller)
            state = (await client.get(f"/api/jobs/{job_id}")).json()
            assert state["phase"] == "restricted" and state["error_code"] == "login_timeout"
            assert (await client.get("/")).status_code == 200
            assert (await client.get(f"/api/jobs/{job_id}/qr")).status_code == 404
            executor.mode = "success"
            await client.post(f"/api/jobs/{job_id}/retry", headers=headers, json={"login": True, "consent": True})
            await wait(controller)
            assert controller.snapshot(job_id)["phase"] == "completed"
            assert executor.calls[-1][:2] == (True, True)
    asyncio.run(execute())


def test_busy_cancel_and_qr_cleanup(tmp_path):
    async def execute():
        executor = Executor(tmp_path)
        executor.mode = "blocked"
        async with environment(tmp_path, executor) as (client, controller, _, headers):
            job_id = (await client.post("/api/jobs", headers=headers, json={"url": "123"})).json()["id"]
            for _ in range(100):
                if controller.snapshot(job_id)["phase"] == "waiting_login":
                    break
                await asyncio.sleep(.001)
            qr = await client.get(f"/api/jobs/{job_id}/qr")
            assert qr.status_code == 200 and qr.content == png()
            assert (await client.post("/api/jobs", headers=headers, json={"url": "456"})).status_code == 409
            assert (await client.post(f"/api/jobs/{job_id}/cancel", headers=headers)).json()["phase"] == "cancelled"
            assert (await client.get(f"/api/jobs/{job_id}/qr")).status_code == 404
            executor.mode = "success"
            assert (await client.post("/api/jobs", headers=headers, json={"url": "456"})).status_code == 202
            await wait(controller)
    asyncio.run(execute())


def test_restart_preserves_target_and_resume_id(tmp_path):
    repository = WebJobRepository(tmp_path / "jobs.sqlite3")
    job = ImageWebJob("saved", {"source": {"type": "detail", "note_ids": ["123"]}, "collect": {"images": {}}},
                      "123", phase="downloading", task_id="persisted-task")
    repository.save(job)

    async def execute():
        executor = Executor(tmp_path)
        async with environment(tmp_path, executor) as (client, controller, _, headers):
            state = (await client.get("/api/jobs/saved")).json()
            assert state["phase"] == "interrupted" and state["task_id"] == "persisted-task"
            await client.post("/api/jobs/saved/retry", headers=headers, json={})
            await wait(controller)
            assert executor.calls[0][2] == "persisted-task"
            assert controller.snapshot("saved")["phase"] == "completed"
    asyncio.run(execute())
