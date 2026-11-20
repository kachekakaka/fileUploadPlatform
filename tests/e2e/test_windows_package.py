import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import socket
import sqlite3
import subprocess
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest

from test_cli_server_worker import wait_until


pytestmark = pytest.mark.skipif(
    not all(
        os.getenv(name)
        for name in (
            "UPLOAD_PLATFORM_TEST_ZIP",
            "UPLOAD_PLATFORM_TEST_MUMU",
            "UPLOAD_PLATFORM_TEST_CHROME",
        )
    ),
    reason="set Windows ZIP, MuMu checkout, and Chrome paths for package verification",
)
ORIGIN = "http://127.0.0.1:8765"
DEPLOYMENT = "X-Upload-Platform-Deployment"


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


class DropAcceptedReply(httpx.AsyncBaseTransport):
    def __init__(self):
        self.accepted = None

    async def handle_async_request(self, request):
        # All requests reach the actual extracted API. Hide only the successful POST reply.
        async with httpx.AsyncHTTPTransport() as transport:
            response = await transport.handle_async_request(request)
            try:
                content = await response.aread()
            finally:
                await response.aclose()
        if request.method == "POST" and request.url.path == "/api/v1/submissions":
            assert response.status_code == 201, content
            self.accepted = json.loads(content)
            raise httpx.ReadError("test dropped an already committed reply", request=request)
        return httpx.Response(response.status_code, headers=response.headers, content=content)


@contextmanager
def launch(bundle, role, env, log, database):
    import win32api
    import win32con
    import win32job

    job = win32job.CreateJobObject(None, "")
    limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    limits["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, limits)
    try:
        with log.open("wb") as stream:
            process = subprocess.Popen(
                [
                    "pwsh",
                    "-NoProfile",
                    "-NonInteractive",
                    "-File",
                    "Start.ps1",
                    role,
                    "-DataDirectory",
                    str(database),
                ],
                cwd=bundle,
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            try:
                handle = win32api.OpenProcess(
                    win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid
                )
                try:
                    win32job.AssignProcessToJobObject(job, handle)
                finally:
                    handle.Close()
                yield process
            finally:
                # Restart uses an abrupt stop to verify durable identity and receipts.
                win32job.TerminateJobObject(job, 1)
                if process.poll() is None:
                    process.terminate()
                process.wait(timeout=15)
    finally:
        job.Close()


def test_unpacked_zip_with_real_mumu_client(tmp_path, monkeypatch):
    playwright = pytest.importorskip("playwright.sync_api")
    monkeypatch.syspath_prepend(os.environ["UPLOAD_PLATFORM_TEST_MUMU"])
    from src.orchestrator.segment_upload_api import Parameters
    from src.orchestrator.upload_manifest import file_snapshot, manifest_for, save_manifest
    from src.orchestrator.upload_platform import BridgeError, UploadPlatform

    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 8765))
    extracted = tmp_path / "干净解压目录"
    with ZipFile(os.environ["UPLOAD_PLATFORM_TEST_ZIP"]) as archive:
        assert archive.testzip() is None
        assert all(
            not name.lower().endswith((".sqlite3", ".mp4", ".log")) for name in archive.namelist()
        )
        archive.extractall(extracted)
    (bundle,) = extracted.iterdir()
    python = bundle / "runtime/python.exe"
    assert "Python 3.13." in subprocess.check_output([str(python), "--version"], text=True)
    spec = json.loads((bundle / "schemas/openapi.json").read_text(encoding="utf-8"))
    health_params = spec["paths"]["/api/v1/system/health"]["get"]["parameters"]
    assert any(item["name"] == DEPLOYMENT and not item["required"] for item in health_params)
    assert spec["info"]["version"] == "0.2.0"

    output = tmp_path / "合成录像"
    sessions = output / "sessions"
    sessions.mkdir(parents=True)
    clips = []
    for name in ("job-b", "job-a"):
        video = output / name / "000001.mp4"
        video.parent.mkdir()
        video.write_bytes(f"synthetic clip from {name}".encode())
        clips.append({"file": file_snapshot(str(video), output)})

    build = "core-protocol-v1-fixture"
    caps = tmp_path / "capabilities.json"
    config = tmp_path / "engine.json"
    control = tmp_path / "control.json"
    gate = tmp_path / "qr-confirmed"
    gate.touch()
    simulated = tmp_path / "simulated-platform"
    simulated.mkdir()
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
    write_json(control, {"qr_gate": str(gate), "archive": str(simulated)})
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.startswith(("UPLOAD_PLATFORM_", "BUE_")) and key != "PYTHONPATH"
    }
    env.update(
        BUE_FIXTURE_CONTROL=str(control),
        UPLOAD_PLATFORM_BILIBILI_CONFIG=str(config),
        UPLOAD_PLATFORM_MANIFEST_ROOTS=str(sessions),
        UPLOAD_PLATFORM_ASSET_ROOTS=str(output),
        UPLOAD_PLATFORM_HEARTBEAT_SECONDS="1",
        UPLOAD_PLATFORM_LEASE_SECONDS="8",
        UPLOAD_PLATFORM_POLL_SECONDS="0.1",
    )
    data = tmp_path / "core-data"
    fresh = tmp_path / "fresh-data"

    def counts(directory):
        with sqlite3.connect((directory / "core.sqlite3").as_uri() + "?mode=ro", uri=True) as conn:
            return [
                conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                for table in ("upload_works", "tasks", "submission_ledger")
            ]

    with httpx.Client(
        base_url=ORIGIN + "/api/v1",
        trust_env=False,
        timeout=3,
        headers={"X-Upload-Platform-Client": "cli-v1"},
    ) as http:

        def health():
            response = http.get("/system/health")
            return response.json() if response.is_success else None

        with launch(bundle, "worker", env, tmp_path / "worker.log", data):
            with launch(bundle, "server", env, tmp_path / "first-server.log", data):
                first = wait_until(health)
                deployment = first["deployment_id"]
                wait_until(lambda: any(item["active"] for item in health()["workers"]))
                with playwright.sync_playwright() as engine:
                    browser = engine.chromium.launch(
                        executable_path=os.environ["UPLOAD_PLATFORM_TEST_CHROME"]
                    )
                    page = browser.new_page()
                    page.goto(ORIGIN)
                    playwright.expect(page.get_by_role("heading", name="概览")).to_be_visible()
                    playwright.expect(page.get_by_text("心跳正常")).to_be_visible()
                    page.screenshot(path=tmp_path / "packaged-dashboard.png")
                    browser.close()

                login = http.post(
                    "/account-login-sessions", json={}, headers={"Idempotency-Key": str(uuid4())}
                )
                assert login.status_code == 202, login.text
                client = UploadPlatform()
                account = wait_until(
                    lambda: next(iter(asyncio.run(client.accounts(deployment))), None)
                )
                assert account["remote_user_id"] == "42"
                asyncio.run(client.panel("accounts"))
                work_id = str(uuid4())
                manifest = manifest_for(
                    output,
                    work_id,
                    {
                        "clips": clips,
                        "account": account,
                        "parameters": Parameters(
                            title="联调合成稿",
                            tags=["合成测试"],
                            category_id=171,
                            copyright=1,
                            publish_mode="self_only",
                        ).model_dump(),
                    },
                )
                path, revision = save_manifest(output, work_id, manifest)
                preview = asyncio.run(
                    client.preview(
                        deployment,
                        {
                            "manifest_path": path,
                            "asset_root": str(output),
                        },
                    )
                )
                assert preview["work_id"] == work_id and preview["config_version"] == revision
                request = {
                    "preview_id": preview["preview_id"],
                    "source": {
                        "application": "mumu",
                        "kind": "integration",
                        "external_reference": "package-check",
                    },
                }
                row = {
                    "deployment": deployment,
                    "request": json.dumps(request),
                    "request_key": str(uuid4()),
                }
                mismatch = http.post(
                    "/submissions",
                    json=request,
                    headers={
                        "Idempotency-Key": row["request_key"],
                        DEPLOYMENT: str(uuid4()),
                    },
                )
                assert (mismatch.status_code, mismatch.json()["code"]) == (409, "binding")
                assert counts(data) == [0, 0, 0]
                wire = DropAcceptedReply()
                with pytest.raises(BridgeError) as dropped:
                    asyncio.run(UploadPlatform(transport=wire).submit(row))
                assert dropped.value.code == "platform_offline"
                expected = wire.accepted
                assert expected["work_id"] == work_id and expected["config_version"] == revision
                assert expected["status"] == "accepted" and len(expected["task_ids"]) == 1
                assert counts(data) == [1, 1, 1]
                Path(path).rename(Path(path).with_suffix(".offline"))

            with launch(bundle, "server", env, tmp_path / "restarted-server.log", data):
                assert wait_until(health)["deployment_id"] == deployment
                recovered = asyncio.run(client.result(row))
                assert recovered == expected
                assert asyncio.run(client.submit(row)) == expected
                task_id = recovered["task_ids"][0]

                def succeeded():
                    task = asyncio.run(client.task(deployment, task_id))
                    return task if task["status"] == "SUCCEEDED" else None

                task = wait_until(succeeded)
                assert task["receipt"]["evidence"]["verified_mid"] == "42"
                assert [entry["relative_path"] for entry in task["snapshot"]["assets"]] == [
                    "job-b/000001.mp4",
                    "job-a/000001.mp4",
                ]
                assert len(list(simulated.glob("*.json"))) == 1
                assert counts(data) == [1, 1, 1]

        with launch(bundle, "server", env, tmp_path / "new-database.log", fresh):
            new = wait_until(health)["deployment_id"]
            assert new != deployment
            reply = http.get(
                "/submissions/result",
                headers={
                    "Idempotency-Key": row["request_key"],
                    DEPLOYMENT: deployment,
                },
            )
            assert reply.status_code == 409 and reply.headers[DEPLOYMENT] == new
            assert counts(fresh) == [0, 0, 0]
        evidence = {
            "deployment_id": deployment,
            "new_deployment_id": new,
            "recovered": recovered,
            "receipt": task["receipt"]["evidence"],
            "original_db_counts": counts(data),
            "new_db_counts": counts(fresh),
            "mismatch_status": mismatch.status_code,
            "reply_dropped_after_http_status": 201,
            "client_error": dropped.value.code,
        }
        write_json(tmp_path / "package-result.json", evidence)
        print(json.dumps(evidence, ensure_ascii=False))
