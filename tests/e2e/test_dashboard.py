import json
import os
from pathlib import Path
import sys
from threading import Thread
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
import uvicorn

from helpers.b18_cases import prepare_cases, write_json
from test_cli_server_worker import Environment, wait_until


pytestmark = pytest.mark.skipif(
    not os.getenv("UPLOAD_PLATFORM_TEST_CHROME"), reason="set path to Chrome for browser checks"
)
DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


def test_dashboard_browser_and_worker(tmp_path, work_factory):
    playwright = pytest.importorskip("playwright.sync_api")
    assert (DIST / "index.html").is_file(), "build web/dist before running the browser test"
    directory, _ = work_factory()
    environment = Environment(tmp_path)
    environment.env.pop("UPLOAD_PLATFORM_BILIBILI_CONFIG", None)
    environment.env.update(
        UPLOAD_PLATFORM_ASSET_ROOTS=str(tmp_path),
        UPLOAD_PLATFORM_MANIFEST_ROOTS=str(tmp_path),
        UPLOAD_PLATFORM_DASHBOARD_DIR=str(DIST),
    )
    parent = FastAPI()

    @parent.get("/external", response_class=HTMLResponse)
    def external():
        return "<!doctype html><title>Independent caller</title>"

    @parent.get("/", response_class=HTMLResponse)
    def embed():
        return f"""<!doctype html><title>Parent fixture</title><output>waiting</output>
        <script>
        addEventListener('message', event => {{
          const frame = document.querySelector('iframe');
          if (event.origin === '{environment.url}' && event.source === frame.contentWindow &&
              event.data?.protocol === 'upload-platform-embed' && event.data.version === 1 &&
              event.data.type === 'ready') {{
            document.querySelector('output').textContent = 'ready';
          }}
        }});
        </script>
        <iframe src="{environment.url}/embed/accounts"></iframe>"""

    import socket

    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        parent_port = sock.getsockname()[1]
    parent_server = uvicorn.Server(
        uvicorn.Config(parent, host="127.0.0.1", port=parent_port, proxy_headers=False)
    )
    parent_thread = Thread(target=parent_server.run, daemon=True)
    parent_thread.start()

    try:
        server_process = environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").is_success)
        origin = environment.url
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(
                executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"],
                args=[f"--host-resolver-rules=MAP 127.0.0.1:8000 127.0.0.1:{parent_port}"],
            )
            context = browser.new_context(viewport={"width": 1320, "height": 900})
            page = context.new_page()
            errors = []
            page.on("pageerror", lambda error: errors.append(str(error)))
            expect = playwright.expect
            page.goto(origin)
            expect(page.get_by_role("heading", name="概览")).to_be_visible()
            expect(page.get_by_text("无活动心跳")).to_be_visible()
            page.screenshot(path=tmp_path / "overview.png")
            for path, title in (("accounts", "账号"), ("tasks", "任务"), ("settings", "设置")):
                page.goto(origin + "/" + path)
                expect(page.get_by_role("heading", name=title, exact=True)).to_be_visible()
                expect(page.get_by_role("alert")).to_have_count(0)
            page.goto(origin + "/works")
            page.get_by_role("button", name="登记本机作品").click()
            page.get_by_label("素材根目录").fill(str(directory))
            page.get_by_role("button", name="预览配置").click()
            expect(page.get_by_text("合并为多 P 稿件")).to_be_visible()
            page.get_by_role("button", name="仅登记作品").click()
            expect(page.get_by_role("link", name="查看作品")).to_be_visible()
            assert environment.http.get("/tasks").json()["items"] == []
            page.get_by_role("button", name="提交上传任务").click()
            task_link = page.get_by_role("link", name="查看任务", exact=False)
            expect(task_link).to_be_visible()
            task_path = task_link.get_attribute("href")
            assert task_path
            task_id = task_path.rsplit("/", 1)[-1]
            task_link.click()
            expect(page.get_by_text("等待执行", exact=True).first).to_be_visible()
            expect(page.get_by_role("heading", name="事件时间线")).to_be_visible()
            page.get_by_role("button", name="取消任务").click()
            page.get_by_role("button", name="确认", exact=True).click()
            expect(page.locator("header .status")).to_have_text("已取消")
            page.get_by_role("button", name="重新提交").click()
            page.get_by_role("button", name="确认", exact=True).click()
            page.get_by_text("已提交新一轮任务").get_by_role("link").click()
            new_task = page.url.rsplit("/", 1)[-1]
            assert new_task != task_id
            environment.start("worker")
            expect(page.locator("header .status")).to_have_text("已成功", timeout=45000)
            expect(page.get_by_text("投稿回执", exact=True)).to_be_visible()
            page.screenshot(path=tmp_path / "task.png")
            tasks = environment.http.get("/tasks", params={"sort": "newest", "limit": 1}).json()
            assert tasks["items"][0]["id"] == new_task
            assert (
                environment.http.get(
                    "/tasks",
                    params={"sort": "newest", "cursor": tasks["next_cursor"], "platform": "fake"},
                ).json()["items"][0]["id"]
                == task_id
            )
            page.goto(origin + "/tasks")
            page.get_by_label("状态").select_option("CANCELLED")
            page.get_by_role("button", name="筛选").click()
            expect(page.locator("tbody tr")).to_have_count(1)
            expect(page.locator("tbody .status")).to_have_text("已取消")
            page.goto(origin + "/schedules")
            page.get_by_role("button", name="新建规则").click()
            page.get_by_label("作品父目录").fill(str(tmp_path))
            page.get_by_role("button", name="保存").click()
            expect(page.get_by_text("每日 09:00:00")).to_be_visible()
            page.get_by_role("button", name="编辑").click()
            page.get_by_label("频率").select_option("weekly")
            page.get_by_label("星期").select_option("3")
            page.get_by_role("button", name="保存").click()
            expect(page.get_by_text("星期三 09:00:00")).to_be_visible()
            page.get_by_role("button", name="停用", exact=True).click()
            expect(page.get_by_text("已停用", exact=False)).to_be_visible()
            page.get_by_role("button", name="启用", exact=True).click()
            expect(page.get_by_text("已启用", exact=False)).to_be_visible()

            clean = browser.new_context()
            embedded = clean.new_page()
            embedded.goto("http://127.0.0.1:8000")
            frame = embedded.frame_locator("iframe")
            for reload in (False, True):
                if reload:
                    embedded.reload()
                expect(embedded.locator("output")).to_have_text("ready")
                expect(frame.get_by_role("heading", name="账号")).to_be_visible()
                expect(frame.get_by_text("还没有 Bilibili 投稿账号")).to_be_visible()
                expect(frame.get_by_role("navigation")).to_have_count(0)
            embedded.evaluate(
                """({origin, task}) => {
                  document.querySelector('iframe').contentWindow.postMessage(
                    {protocol:'upload-platform-embed', version:2, type:'open-task',
                     payload:{task_id:task}}, origin
                  );
                }""",
                {"origin": origin, "task": new_task},
            )
            expect(frame.get_by_role("heading", name="账号")).to_be_visible()
            embedded.evaluate(
                """({origin, task}) => {
              const frame = document.querySelector('iframe');
              frame.contentWindow.postMessage({protocol:'upload-platform-embed',version:1,
                type:'open-task',payload:{task_id:task}}, origin);
            }""",
                {"origin": origin, "task": new_task},
            )
            expect(frame.get_by_role("heading", name="任务快照")).to_be_visible()
            expect(frame.locator("header .status")).to_have_text("已成功")
            next_page = embedded.frame_locator("iframe")
            task_frame = next(
                candidate for candidate in embedded.frames if "/embed/tasks/" in candidate.url
            )
            task_frame.evaluate("location.reload()")
            expect(next_page.locator("header .status")).to_have_text("已成功")
            assert not clean.cookies()

            external_directory, _ = work_factory()
            independent = browser.new_page()
            independent.goto(f"http://127.0.0.1:{parent_port}/external")
            requests = []
            network = independent.context.new_cdp_session(independent)
            network.send("Network.enable")
            network.on("Network.requestWillBeSent", lambda event: requests.append(event))
            outside_post = independent.evaluate(
                """async ({origin, directory, manifest}) => {
                      const url = origin + '/api/v1/submissions';
                      const body = { asset_root: directory, manifest_path: manifest };
                      const headers = { 'Content-Type': 'application/json' };
                      const preview = await (await fetch(url + '/preview', {
                        method:'POST', credentials:'omit', headers, body:JSON.stringify(body)
                      })).json();
                      const key = crypto.randomUUID();
                      const post = await fetch(url, {
                        method:'POST', credentials:'omit',
                        headers:{...headers, 'Idempotency-Key':key},
                        body:JSON.stringify({preview_id:preview.preview_id})
                      });
                      const lookup = await fetch(url + '/result', {
                        credentials:'omit', headers:{'Idempotency-Key':key}
                      });
                      const missing = await fetch(url + '/result', {
                        credentials:'omit', headers:{'Idempotency-Key':crypto.randomUUID()}
                      });
                      return {key, status:post.status, accepted:await post.json(),
                              found:await lookup.json(), missing:await missing.json()};
                    }""",
                {
                    "origin": origin,
                    "directory": str(external_directory),
                    "manifest": str(external_directory / "upload.yaml"),
                },
            )
            assert any(
                sent["request"]["method"] == "OPTIONS"
                and sent["request"]["url"].endswith("/submissions")
                for sent in requests
            )
            assert outside_post["status"] == 201
            assert outside_post["accepted"] == outside_post["found"]
            outside_task = outside_post["accepted"]["task_ids"][0]
            assert outside_post["missing"]["code"] == "submission_not_found"
            assert outside_task != new_task
            assert len(environment.http.get("/tasks").json()["items"]) == 3
            wait_until(
                lambda: (
                    environment.http.get("/tasks/" + outside_task).json()["status"] == "SUCCEEDED"
                )
            )
            config_file = external_directory / "upload.yaml"
            config_file.rename(config_file.with_suffix(".offline"))
            server_process.terminate()
            server_process.wait(timeout=10)
            environment.start("server")
            wait_until(lambda: environment.http.get("/system/health").is_success)
            restored = environment.http.get(
                "/submissions/result", headers={"Idempotency-Key": outside_post["key"]}
            )
            assert restored.status_code == 200 and restored.json() == outside_post["accepted"]
            assert len(environment.http.get("/tasks").json()["items"]) == 3
            assert not errors, errors
            browser.close()
        assert environment.http.get("/tasks/" + new_task).json()["status"] == "SUCCEEDED"
    finally:
        environment.close()
        parent_server.should_exit = True
        parent_thread.join(8)
        assert not parent_thread.is_alive()


def test_dashboard_browser_login_fixture(tmp_path):
    playwright = pytest.importorskip("playwright.sync_api")
    assert (DIST / "index.html").is_file(), "build web/dist before running the browser test"
    environment = Environment(tmp_path)
    build = "core-protocol-v1-fixture"
    capabilities = tmp_path / "capabilities.json"
    capabilities.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "engine_build_id": build,
                "submit_api": "web",
                "login_interaction": {
                    "version": 1,
                    "modes": ["terminal", "machine"],
                    "image_type": "image/png",
                },
            }
        ),
        encoding="utf-8",
    )
    config = tmp_path / "engine.json"
    config.write_text(
        json.dumps(
            {
                "executable": sys.executable,
                "build_id": build,
                "state_root": str(tmp_path / "private"),
                "capabilities_file": str(capabilities),
                "allow_test_engine": True,
                "test_arguments": [
                    str(Path(__file__).resolve().parents[1] / "helpers" / "bilibili_fixture.py")
                ],
            }
        ),
        encoding="utf-8",
    )
    control = tmp_path / "control.json"
    gate = tmp_path / "confirm"
    control.write_text(json.dumps({"qr_gate": str(gate)}), encoding="utf-8")
    environment.env.update(
        UPLOAD_PLATFORM_BILIBILI_CONFIG=str(config),
        BUE_FIXTURE_CONTROL=str(control),
        UPLOAD_PLATFORM_DASHBOARD_DIR=str(DIST),
    )
    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").is_success)
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(
                executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"]
            )
            context = browser.new_context()
            page = context.new_page()
            expect = playwright.expect
            page.goto(environment.url + "/accounts")
            add = page.get_by_role("button", name="添加 Bilibili 账号")
            expect(add).to_be_enabled()
            assert environment.http.get("/account-login-sessions").json() == {"items": []}
            add.click()
            page.get_by_role("button", name="开始扫码", exact=True).click()
            qr = page.get_by_role("img", name="Bilibili 扫码登录二维码")
            expect(qr).to_be_visible(timeout=12000)
            first = qr.get_attribute("src")
            assert first
            image = environment.http.get(first.removeprefix("/api/v1"))
            assert image.status_code == 200 and image.headers["cache-control"] == "no-store"
            page.reload()
            expect(qr).to_have_attribute("src", first)
            page.get_by_role("button", name="取消扫码").click()
            expect(page.get_by_text("已取消", exact=True)).to_be_visible(timeout=12000)
            expect(qr).to_have_count(0)
            page.get_by_role("button", name="重新扫码").click()
            expect(qr).to_be_visible(timeout=12000)
            assert qr.get_attribute("src") != first
            gate.touch()
            expect(page.get_by_text("登录完成")).to_be_visible(timeout=12000)
            expect(page.get_by_text("MID 42")).to_be_visible()
            expect(qr).to_have_count(0)
            assert not context.cookies()
            assert page.url.endswith("/accounts")
            browser.close()
    finally:
        environment.close()


def test_dashboard_bilibili_601_keeps_the_original_task_uncertain(tmp_path, work_factory):
    playwright = pytest.importorskip("playwright.sync_api")
    assert (DIST / "index.html").is_file(), "build web/dist before running the browser test"
    cases = prepare_cases(tmp_path, work_factory)
    config = tmp_path / "engine.json"
    engine = json.loads(config.read_text(encoding="utf-8"))
    engine.pop("b18_allowlist_file")
    write_json(config, engine)
    caps = tmp_path / "capabilities.json"
    capabilities = json.loads(caps.read_text(encoding="utf-8"))
    write_json(caps, {**capabilities, "upload_ready": True, "verified_publish_modes": ["public"]})
    control = tmp_path / "control.json"
    environment = Environment(tmp_path)
    environment.env.update(
        UPLOAD_PLATFORM_BILIBILI_CONFIG=str(config),
        BUE_FIXTURE_CONTROL=str(control),
        UPLOAD_PLATFORM_DASHBOARD_DIR=str(DIST),
    )
    hint = "B 站尚未确认收稿，可能需要本人验证。请先查回原任务，避免重复投稿。"
    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").is_success)
        assert environment.cli("account", "login")["operation_result"]["status"] == "SUCCEEDED"
        worker = environment.start("worker")
        paths = []
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(
                executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"]
            )
            page = browser.new_page()
            for name, code in (("single", 601), ("multi", 602)):
                case = cases[name]
                write_json(control, {"submit_response_code": code, "query_failure": True})
                environment.cli("work", "register", "--dir", case["directory"])
                response = environment.http.post(
                    f"/works/{case['work_id']}/submit",
                    headers={"Idempotency-Key": str(uuid4())},
                    json={"retry_policy": {"max_attempts": 2, "retry_delays_seconds": [0]}},
                )
                response.raise_for_status()
                path = "/tasks/" + response.json()["tasks"][0]["task_id"]
                paths.append(path)
                wait_until(lambda: environment.http.get(path).json()["status"] == "UNCERTAIN")
                wait_until(
                    lambda: any(
                        q["status"] == "FINISHED"
                        for q in environment.http.get(path + "/queries").json()["items"]
                    )
                )
                page.goto(environment.url + path)
                playwright.expect(page.locator(".status")).to_have_text("结果待核验")
                if code == 601:
                    for reload in (False, True):
                        if reload:
                            page.reload()
                        playwright.expect(page.get_by_text(hint, exact=True)).to_be_visible()
                    worker.terminate()
                    worker.wait(timeout=10)
                    environment.start("worker")
                else:
                    playwright.expect(
                        page.get_by_text("远端结果待核验。", exact=False)
                    ).to_be_visible()
                    playwright.expect(page.get_by_text(hint, exact=True)).to_have_count(0)
            browser.close()
        for path in paths:
            task = environment.http.get(path).json()
            assert task["status"] == "UNCERTAIN" and task["attempt_count"] == 1
            assert task["receipt"] is None
        assert len(list((tmp_path / "data/executions").glob("*/fixture-received.json"))) == 2
    finally:
        environment.close()


def test_works_recovers_dropped_receipt_after_reload_and_navigation(tmp_path, work_factory):
    playwright = pytest.importorskip("playwright.sync_api")
    assert (DIST / "index.html").is_file(), "build web/dist before running the browser test"
    environment = Environment(tmp_path)
    environment.env.pop("UPLOAD_PLATFORM_BILIBILI_CONFIG", None)
    environment.env.update(
        UPLOAD_PLATFORM_MANIFEST_ROOTS=str(tmp_path),
        UPLOAD_PLATFORM_ASSET_ROOTS=str(tmp_path),
        UPLOAD_PLATFORM_DASHBOARD_DIR=str(DIST),
    )
    fixtures = [work_factory()[0] for _ in range(2)]

    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").is_success)
        with playwright.sync_playwright() as runner:
            browser = runner.chromium.launch(
                executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"]
            )
            context = browser.new_context()
            page = context.new_page()
            expect = playwright.expect
            for index, directory in enumerate(fixtures):
                sent, accepted = [], []

                def intercept(route):
                    request = route.request
                    key = request.headers["idempotency-key"]
                    sent.append(
                        (key, request.post_data, request.headers["x-upload-platform-deployment"])
                    )
                    if len(sent) > 1:
                        route.continue_()
                        return
                    persisted = page.evaluate(
                        "key => JSON.parse(localStorage.getItem('upload-platform.submission.v1.' + key))",
                        key,
                    )
                    assert persisted["body"] == request.post_data
                    assert persisted["deployment"] == sent[-1][2]
                    response = route.fetch()
                    assert response.status == 201
                    accepted.append(response.json())
                    route.abort("connectionreset")

                page.goto(environment.url + "/works")
                page.route("**/api/v1/submissions", intercept)
                page.get_by_role("button", name="登记本机作品").click()
                page.get_by_label("素材根目录").fill(str(directory))
                page.get_by_role("button", name="预览配置").click()
                page.get_by_role("button", name="提交上传任务").click()
                expect(page.get_by_role("button", name="查回原请求")).to_be_visible()
                assert len(accepted) == 1
                task_id = accepted[0]["task_ids"][0]

                if index == 0:
                    page.reload()
                    expect(page.get_by_role("button", name="查回原请求")).to_be_visible()
                    page.get_by_role("button", name="查回原请求").click()
                    assert len(sent) == 1
                else:
                    page.get_by_role("link", name="概览", exact=True).click()
                    expect(page.get_by_role("heading", name="概览", exact=True)).to_be_visible()
                    page.get_by_role("link", name="作品", exact=True).click()
                    expect(page.get_by_role("button", name="重试原请求")).to_be_visible()
                    page.get_by_role("button", name="重试原请求").click()

                link = page.get_by_role("link", name="查看任务", exact=False)
                expect(link).to_have_attribute("href", "/tasks/" + task_id)
                if index == 1:
                    assert len(sent) == 2 and sent[0] == sent[1]
                assert len(environment.http.get("/tasks").json()["items"]) == index + 1
                page.unroute("**/api/v1/submissions", intercept)
            browser.close()
    finally:
        environment.close()
