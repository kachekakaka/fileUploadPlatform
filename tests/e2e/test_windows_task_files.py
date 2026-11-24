"""Exercise task-selected media across a real API restart and loopback transfer."""

import asyncio
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import socket
import subprocess
import threading
from types import SimpleNamespace
from uuid import UUID, uuid4
from zipfile import ZipFile

import httpx
import pytest
import uvicorn

from test_cli_server_worker import wait_until
from test_windows_package import ORIGIN, launch, write_json


pytestmark = pytest.mark.skipif(
    not all(
        os.getenv("UPLOAD_PLATFORM_TEST_" + field) for field in ("ZIP", "MUMU", "CHROME", "FFMPEG")
    ),
    reason="supply the Windows ZIP, MuMu with its built console, Chrome and FFmpeg",
)


@contextmanager
def receiver(directory):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(b"<title>Independent local test page</title>")

        def do_PUT(self):
            task, part = self.path.strip("/").split("/")
            assert task == str(UUID(task)) and part in {"1", "2"}
            assert self.headers["Content-Type"] == "video/mp4"
            body = self.rfile.read(int(self.headers["Content-Length"]))
            assert body
            if task == self.server.retry and not self.server.rejected:
                self.server.rejected.append(task)
                status = 503
            else:
                with (directory / f"{task}-{part}.mp4").open("xb") as output:
                    output.write(body)
                self.server.accepted.append((task, int(part)))
                status = 201
            self.send_response(status)
            self.end_headers()

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        server.retry = None
        server.accepted, server.rejected = [], []
        thread = threading.Thread(target=server.serve_forever)
        thread.start()
        try:
            yield server, f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def fixture_env(tmp_path, python, origin):
    caps = tmp_path / "capabilities.json"
    config = tmp_path / "fixture.json"
    control = tmp_path / "control.json"
    gate = tmp_path / "qr-confirmed"
    gate.touch()
    build = "core-protocol-v1-fixture"
    write_json(
        caps,
        {
            "protocol_version": 1,
            "engine_build_id": build,
            "submit_api": "web",
            "upload_ready": True,
            "verified_publish_modes": ["self_only"],
            "supports_self_only": True,
            "login_interaction": {
                "version": 1,
                "modes": ["terminal", "machine"],
                "image_type": "image/png",
            },
            "metadata_limits": {
                "title": 80,
                "part_title": 80,
                "description": 2000,
                "tag": 20,
                "tag_count": 10,
                "part_count": 10,
            },
        },
    )
    write_json(
        config,
        {
            "executable": str(python),
            "build_id": build,
            "state_root": str(tmp_path / "fixture-private"),
            "capabilities_file": str(caps),
            "allow_test_engine": True,
            "allow_self_only": True,
            "test_arguments": [
                str(Path(__file__).resolve().parents[1] / "helpers/bilibili_fixture.py")
            ],
        },
    )
    write_json(control, {"qr_gate": str(gate), "media_receiver": origin})
    return {
        **{
            key: value
            for key, value in os.environ.items()
            if not key.upper().startswith(("UPLOAD_PLATFORM_", "BUE_", "PYTHON"))
        },
        "BUE_FIXTURE_CONTROL": str(control),
        "UPLOAD_PLATFORM_BILIBILI_CONFIG": str(config),
        "UPLOAD_PLATFORM_HEARTBEAT_SECONDS": "1",
        "UPLOAD_PLATFORM_LEASE_SECONDS": "8",
        "UPLOAD_PLATFORM_POLL_SECONDS": "0.1",
        # Existing launcher environment can still contain paths that were moved away.
        "UPLOAD_PLATFORM_MANIFEST_ROOTS": str(tmp_path / "removed-config"),
        "UPLOAD_PLATFORM_ASSET_ROOTS": str(tmp_path / "removed-media"),
    }


@contextmanager
def serve_mumu(app):
    with socket.socket() as listener:
        # Serve the console over real loopback HTTP so Chrome sees its local network scope.
        listener.bind(("127.0.0.1", 8000))
        listener.listen(128)
        server = uvicorn.Server(uvicorn.Config(app, lifespan="off", log_level="error"))
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]})
        thread.start()
        try:
            wait_until(lambda: server.started)
            yield
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            assert not thread.is_alive()


def verify_browser(playwright, jobs, task_ids, origin, external, deployment):
    with playwright.sync_playwright() as tools:
        browser = tools.chromium.launch(executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"])
        try:
            context = browser.new_context()
            page = context.new_page()
            for job, task in zip(jobs, task_ids, strict=True):
                page.goto(f"http://127.0.0.1:8000/segments?job_id={job}")
                page.get_by_role("row").filter(has_text="001.mp4").get_by_role(
                    "button", name="查看任务", exact=True
                ).click()
                playwright.expect(page).to_have_url(f"http://127.0.0.1:8000/uploads/tasks/{task}")
                frame = page.locator("iframe")
                playwright.expect(frame).to_have_attribute(
                    "src", f"{ORIGIN}/embed/tasks/{task}?deployment={deployment}"
                )
                playwright.expect(
                    page.frame_locator("iframe").locator("header .status")
                ).to_have_text("已成功")
            page.goto(origin)
            response = page.evaluate(
                """async ({api, paths}) => {
                    const headers = {'Content-Type':'application/json'};
                    const preview = await fetch(api + '/preview', {
                        method:'POST', credentials:'omit', headers, body:JSON.stringify(paths)
                    });
                    const staged = await preview.json();
                    const post = await fetch(api, {
                        method:'POST', credentials:'omit', body:JSON.stringify({preview_id:staged.preview_id}),
                        headers:{...headers, 'Idempotency-Key':crypto.randomUUID()}
                    });
                    return {preview:preview.status, status:post.status, receipt:await post.json()};
                }""",
                {"api": ORIGIN + "/api/v1/submissions", "paths": external},
            )
            assert (response["preview"], response["status"]) == (200, 201), response
            return response["receipt"]["task_ids"][0]
        finally:
            browser.close()


def test_task_files_reach_receiver_and_mumu_reads_original_slices(
    tmp_path, monkeypatch, work_factory
):
    playwright = pytest.importorskip("playwright.sync_api")
    checkout = Path(os.environ["UPLOAD_PLATFORM_TEST_MUMU"])
    monkeypatch.syspath_prepend(
        os.environ.get("UPLOAD_PLATFORM_TEST_MUMU_HELPERS", str(checkout / "tests"))
    )
    monkeypatch.syspath_prepend(str(checkout))
    from src.orchestrator.segment_uploads import SegmentUploads
    from src.orchestrator import recording_archive
    from src.orchestrator.server import Runtime, ServerConfig, create_app
    from src.orchestrator.session_deliveries import SessionDeliveries
    from src.orchestrator.storage import Store
    from src.orchestrator.upload_platform import UploadPlatform
    from test_session_deliveries import (
        FIELDS,
        STARTED,
        add_job,
        capture,
        current,
        finish_clip,
        finish_worker,
        room,
        start_clip,
    )

    with ZipFile(os.environ["UPLOAD_PLATFORM_TEST_ZIP"]) as archive:
        assert archive.testzip() is None
        archive.extractall(tmp_path / "解压")
    (bundle,) = (tmp_path / "解压").iterdir()
    ffmpeg = os.environ["UPLOAD_PLATFORM_TEST_FFMPEG"]

    def paint(path, color):
        subprocess.run(
            [
                ffmpeg,
                "-nostdin",
                "-v",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={color}:s=64x64:r=10:d=2",
                "-c:v",
                "mpeg4",
                str(path),
            ],
            check=True,
            timeout=15,
        )

    def decodes(path):
        return (
            subprocess.run(
                [
                    ffmpeg,
                    "-nostdin",
                    "-v",
                    "error",
                    "-xerror",
                    "-i",
                    str(path),
                    "-f",
                    "null",
                    os.devnull,
                ],
                timeout=15,
            ).returncode
            == 0
        )

    data = tmp_path / "core"
    console_dir = tmp_path / "mumu"
    first_root, second_root = tmp_path / "手动素材", tmp_path / "自动素材"
    console_dist = Path(
        os.environ.get("UPLOAD_PLATFORM_TEST_MUMU_WEB", str(checkout / "web" / "dist"))
    )
    assert (console_dist / "index.html").is_file(), f"build the MuMu console in {console_dist}"
    console_config = ServerConfig(console_dir, first_root, static_root=console_dist)
    runtime = Runtime(console_config)
    runtime.start = lambda: None
    runtime.close = lambda: None
    runtime.state = "running"
    ledger = runtime.store = Store(console_dir / "orchestrator.sqlite3")

    def record_sample(target, root, room_id, colors):
        observed = replace(room(number=room_id), web_rid=f"fixture-{room_id}")
        job = add_job(ledger, target, observed, STARTED - 1)
        assert job
        staged = capture(ledger, job, root)
        for index, color in enumerate(colors, 1):
            clip = staged / f"part_{index:06d}.mp4"
            at = STARTED + 2 * (index - 1)
            start_clip(ledger, job, index, clip, at)
            paint(clip, color)
            finish_clip(ledger, job, index, clip, at + 2)
        ledger.request_stop(job, "manual_stop")
        finish_worker(ledger, job, STARTED + 2 * len(colors) + 1)
        recording_archive.publish(ledger)
        with ledger.transaction() as connection:
            connection.execute(
                "UPDATE recording_sessions SET title_step='done' WHERE target_id=?", (target,)
            )
        return job

    core = UploadPlatform()
    downloads = tmp_path / "接收端"
    downloads.mkdir()
    try:
        with (
            receiver(downloads) as (sink, origin),
            httpx.Client(
                base_url=ORIGIN + "/api/v1",
                timeout=5,
                trust_env=False,
                limits=httpx.Limits(max_keepalive_connections=0),
                headers={"X-Upload-Platform-Client": "cli-v1"},
            ) as http,
        ):
            env = fixture_env(tmp_path, bundle / "runtime/python.exe", origin)

            def task(task_id):
                reply = http.get(f"/tasks/{task_id}")
                reply.raise_for_status()
                return reply.json()

            with launch(bundle, "server", env, tmp_path / "first-api.log", data):
                deployment = wait_until(lambda: http.get("/system/health").json())["deployment_id"]
                write_json(console_dir / "settings.json", {"upload_deployment": deployment})
                response = http.post(
                    "/account-login-sessions", json={}, headers={"Idempotency-Key": str(uuid4())}
                )
                assert response.status_code == 202, response.text
                account = wait_until(
                    lambda: next(iter(asyncio.run(core.accounts(deployment))), None)
                )

                manual = SegmentUploads(runtime, core, first_root)
                manual_target = ledger.add_target("fixture-manual")
                first_job = record_sample(manual_target, first_root, "1200", ("red",))
                draft = asyncio.run(manual.preview(first_job, 1, deployment, account, FIELDS))
                first = asyncio.run(manual.confirm(draft["ticket"], True))["task_id"]

                automatic = SegmentUploads(runtime, core, second_root)
                automatic_target = ledger.add_target("fixture-auto")
                ledger.save_posting_settings(
                    automatic_target,
                    True,
                    {
                        "deployment": deployment,
                        "category_id": 171,
                        "account": account,
                    },
                )
                second_job = record_sample(automatic_target, second_root, "1201", ("blue", "lime"))
                session = SessionDeliveries(
                    runtime,
                    automatic,
                    verify_media=lambda path: SimpleNamespace(
                        is_valid=decodes(path), reject_reason="decode failed"
                    ),
                )
                for _ in range(2):
                    asyncio.run(session.deliver(current(ledger, automatic_target)))
                (arrangement,) = automatic.store.for_segments([(second_job, 1)])
                second = arrangement["task_id"]
                assert first and second and first != second
                assert [task(value)["status"] for value in (first, second)] == ["QUEUED", "QUEUED"]
                manual_path, automatic_path = (Path(task(value)["snapshot"]["manifest_path"])
                                               for value in (first, second))
                assert manual_path == Path(current(ledger, manual_target)["recording_directory"]) / "投稿/001/upload.yaml"
                assert automatic_path == Path(current(ledger, automatic_target)["recording_directory"]) / "upload.yaml"
                assert sink.accepted == []
                sink.retry = second
                for path in (task(value)["snapshot"]["manifest_path"] for value in (first, second)):
                    file = Path(path)
                    file.rename(file.with_suffix(".offline"))

            with (
                launch(bundle, "server", env, tmp_path / "restarted-api.log", data),
                launch(bundle, "worker", env, tmp_path / "worker.log", data),
            ):
                assert (
                    wait_until(lambda: http.get("/system/health").json())["deployment_id"]
                    == deployment
                )
                assert wait_until(lambda: task(first)["status"] == "SUCCEEDED", timeout=55)
                wait_until(lambda: task(second)["status"] == "RETRY_WAIT", timeout=30)
                assert task(second)["attempt"]["result"]["submission_state"] == "not_submitted"
                assert wait_until(lambda: task(second)["status"] == "SUCCEEDED", timeout=65)
                assert [task(value)["attempt_count"] for value in (first, second)] == [1, 2]
                assert sink.accepted == [(first, 1), (second, 1), (second, 2)] and sink.rejected == [second]
                for task_id, part, index in ((first, 1, 0), (second, 1, 2), (second, 2, 1)):
                    file = downloads / f"{task_id}-{part}.mp4"
                    assert decodes(file)
                    pixel = subprocess.check_output(
                        [
                            ffmpeg,
                            "-nostdin",
                            "-v",
                            "error",
                            "-i",
                            str(file),
                            "-frames:v",
                            "1",
                            "-vf",
                            "scale=1:1,format=rgb24",
                            "-f",
                            "rawvideo",
                            "pipe:1",
                        ],
                        timeout=12,
                    )
                    assert (
                        len(pixel) == 3
                        and pixel[index] > 220
                        and all(component < 35 for i, component in enumerate(pixel) if i != index)
                    )
                external, _ = work_factory()
                with serve_mumu(create_app(console_config, runtime, platform=core)):
                    browser_task = verify_browser(
                        playwright,
                        (first_job, second_job),
                        (first, second),
                        origin,
                        {
                            "manifest_path": str(external / "upload.yaml"),
                            "asset_root": str(external),
                        },
                        deployment,
                    )
                assert browser_task not in (first, second)
                wait_until(lambda: task(browser_task)["status"] == "SUCCEEDED")
                results = {
                    "deployment_id": deployment,
                    "manual_task": first,
                    "automatic_task": second,
                    "independent_browser_task": browser_task,
                    "receiver_colors": ["red", "blue"],
                    "receiver_attempts": [1, 2],
                    "mumu_opened_original_tasks": [first, second],
                }
                write_json(tmp_path / "task-files-result.json", results)
                print(json.dumps(results, ensure_ascii=False))
    finally:
        ledger.close()
