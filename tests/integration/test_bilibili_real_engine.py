"""Cross-repository contract test: real Rust, real Core, local HTTP only."""

import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
import queue
import subprocess
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from conftest import save_manifest
from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.api.app import create_app
from upload_platform.application.accounts import Accounts, MaintenanceRequest, ProcessIdentity
from upload_platform.application.contracts import Registration, RetryPolicy, Submission
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.application.service import Service
from upload_platform.domain.core import uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure.database import Database
from upload_platform.infrastructure.windows import JobHost


pytestmark = pytest.mark.skipif(
    not os.getenv("BUE_TEST_EXECUTABLE") or not os.getenv("BUE_HTTP_TEST_MODULE"),
    reason="requires separately built fixture Rust engine and its HTTP fixture module",
)


@pytest.fixture
def real_bilibili(config, tmp_path, monkeypatch):
    executable = Path(os.environ["BUE_TEST_EXECUTABLE"])
    capabilities = json.loads(subprocess.check_output([str(executable), "capabilities"], text=True))
    assert capabilities["engine_build_id"].endswith("-fixture")
    capability_file = tmp_path / "capabilities.json"
    capability_file.write_text(json.dumps(capabilities))
    spec = importlib.util.spec_from_file_location(
        "bue_http_fixture", os.environ["BUE_HTTP_TEST_MODULE"]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.server.__wrapped__()
    server = next(fixture)
    monkeypatch.setenv("BUE_PROTOCOL_FIXTURE", server[0])
    database = Database(
        replace(
            config,
            bilibili=EngineConfig(
                executable,
                capabilities["engine_build_id"],
                tmp_path / "private",
                capability_file,
                True,
            ),
        )
    )
    service = Service(database)
    accounts = Accounts(service)
    try:
        database.initialize()
        operation = accounts.create(uid(), MaintenanceRequest())[1]
        started = accounts.start(operation["id"], uid())[1]

        def persist(value):
            accounts.process(
                operation["id"],
                uid(),
                ProcessIdentity(**{k: value[k] for k in ("pid", "creation_time", "assigned")}),
            )

        host = JobHost(started["execution"], persist, command=started["command"])
        try:
            host.send(started["engine_request"])
            deadline = time.monotonic() + 20
            while (not host.exited() or host.active_count()) and time.monotonic() < deadline:
                try:
                    line = host.messages.get(timeout=0.05)
                except queue.Empty:
                    continue
                if line and json.loads(line).get("type") == "account_identified":
                    identity = accounts.identified(operation["id"], uid())[1]
                    assert identity["ready"]
                    host.send(
                        {
                            "type": "maintenance_ready",
                            "operation_id": operation["id"],
                            "verified_mid": identity["verified_mid"],
                        }
                    )
            assert host.exited() and host.active_count() == 0
            assert accounts.complete(operation["id"], uid())[1]["status"] == "SUCCEEDED"
        finally:
            if not host.exited() or host.active_count():
                host.stop()
            host.close()
        yield service, server
    finally:
        database.close()
        fixture.close()


@pytest.fixture
def bilibili_work(work_factory):
    def create(**options):
        directory, manifest = work_factory()
        manifest["work"].pop("cover", None)
        for video in manifest["assets"]["videos"]:
            video.pop("subtitles", None)
        manifest["upload_units"][0]["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:42",
                "options": {
                    "category_id": 171,
                    "copyright": 1,
                    "publish_mode": "public",
                    **options,
                },
            }
        ]
        save_manifest(directory, manifest)
        return directory, manifest

    return create


@pytest.fixture
def ordinary_api(real_bilibili):
    service, _ = real_bilibili
    database = service.database
    with TestClient(
        create_app(database.config, database, service),
        base_url=database.config.api_url,
        headers={"X-Upload-Platform-Client": "cli-v1"},
    ) as api:
        yield api


def post(api, path, body, key=None):
    return api.post(f"/api/v1{path}", json=body, headers={"Idempotency-Key": key or uid()})


@pytest.mark.parametrize(
    "second_part_failure, hide, mode",
    [
        (False, False, "public"),
        (False, True, "public"),
        (True, False, "public"),
        (False, False, "self_only"),
    ],
    ids=["accepted", "space-hidden", "p2-complete", "self-only"],
)
def test_real_core_and_rust_login_upload_query_contract(
    real_bilibili, bilibili_work, second_part_failure, hide, mode
):
    service, server = real_bilibili
    directory, manifest = bilibili_work(publish_mode=mode, hide_in_space=hide)
    options = manifest["upload_units"][0]["targets"][0]["options"]
    key = uid()
    registration = service.registration(
        key, Registration(directory=str(directory), manifest=manifest)
    )
    if not hide:
        options.pop("hide_in_space")
        assert (
            service.registration(key, Registration(directory=str(directory), manifest=manifest))
            == registration
        )
    submission = Submission(retry_policy=RetryPolicy(max_attempts=1, retry_delays_seconds=[]))
    task_id = service.submit(manifest["work"]["id"], uid(), submission)[1]["tasks"][0]["task_id"]
    store = ExecutionStore(service.database)
    store.register()
    if second_part_failure:
        server[1]["bucket_fault"] = {
            "path": "/bucket/key2.mp4",
            "step": "complete",
            "remaining": 3,
            "status": 503,
        }
    Supervisor(store).execute(store.claim())
    task = service.task(task_id)
    if second_part_failure:
        assert task["status"] == "FAILED", task
        assert task["attempt_count"] == 1 and task["attempt"]["exit_confirmed"]
        assert task["attempt"]["result"]["error"] == {
            "code": "VIDEO_UPLOAD_FAILED_P2_COMPLETE_HTTP_503"
        }
        assert task["attempt"]["result"]["submission_state"] == "not_submitted"
        assert task["receipt"] is None and server[1]["submissions"] == []
        assert server[2] not in json.dumps(task)
        return
    assert task["status"] == "SUCCEEDED", task
    assert not task["receipt"]["fake"] and task["receipt"]["evidence"]["aid"] == 123
    server[1]["review_state"] = -4
    assert RemoteQueries(service).query(task_id, uid())[1]["result"]["review_status"] == -4
    [posted] = server[1]["submissions"]
    assert posted["space_hidden"] == (1 if hide else 2)
    assert posted["is_only_self"] == (1 if mode == "self_only" else 0)
    assert task["snapshot"]["target"]["options"].get("hide_in_space", False) is hide
    assert server[2] not in json.dumps(service.task(task_id))


@pytest.mark.parametrize(
    ("options", "disabled", "error_code"),
    [
        ({}, {"upload_ready": False}, "UNSUPPORTED_OPTION"),
        (
            {"publish_mode": "self_only"},
            {"verified_publish_modes": ["public"]},
            "UNSUPPORTED_OPTION",
        ),
        ({"publish_mode": "self_only"}, {"supports_self_only": False}, "UNSUPPORTED_OPTION"),
        ({"hide_in_space": True}, {"supports_hide_in_space": False}, "UNSUPPORTED_OPTION"),
        ({}, {"engine_build_id": "different-fixture"}, "engine_incompatible"),
    ],
    ids=[
        "uploads-closed",
        "mode-not-verified",
        "private-unsupported",
        "hide-unsupported",
        "wrong-build",
    ],
)
def test_ordinary_posting_checks_capabilities_at_each_stage(
    real_bilibili, ordinary_api, bilibili_work, options, disabled, error_code
):
    service, (_, platform, _) = real_bilibili
    database = service.database
    caps_file = database.config.bilibili.capabilities_file
    enabled_caps = caps_file.read_text(encoding="utf-8")
    disabled_caps = json.dumps({**json.loads(enabled_caps), **disabled})
    directory, manifest = bilibili_work(**options)
    route = f"/works/{manifest['work']['id']}"
    registration = {"directory": str(directory), "manifest": manifest}
    platform["calls"].clear()

    def require_denial(path, body):
        response = post(ordinary_api, path, body)
        assert response.status_code == 409 and response.json()["code"] == error_code, response.text

    caps_file.write_text(disabled_caps, encoding="utf-8")
    require_denial("/works", registration)
    assert ordinary_api.get("/api/v1/works").json()["items"] == []

    caps_file.write_text(enabled_caps, encoding="utf-8")
    assert post(ordinary_api, "/works", registration).status_code == 201
    caps_file.write_text(disabled_caps, encoding="utf-8")
    require_denial(f"{route}/preview", {})
    require_denial(f"{route}/submit", {})
    assert ordinary_api.get("/api/v1/tasks").json()["items"] == []

    caps_file.write_text(enabled_caps, encoding="utf-8")
    response = post(ordinary_api, f"{route}/submit", {})
    assert response.status_code == 201
    [created] = response.json()["tasks"]
    caps_file.write_text(disabled_caps, encoding="utf-8")
    store = ExecutionStore(database)
    store.register()
    Supervisor(store).execute(store.claim())
    denied = service.task(created["task_id"])
    assert denied["status"] == "FAILED" and denied["attempt"]["error"] == error_code
    assert denied["attempt"]["handed_off_at"] is None and denied["receipt"] is None
    assert not platform["calls"]


def test_ordinary_posting_response_loss_recovers_without_reposting(
    real_bilibili, ordinary_api, bilibili_work, tmp_path
):
    service, (_, platform, _) = real_bilibili
    directory, manifest = bilibili_work()
    registration = {"directory": str(directory), "manifest": manifest}
    assert post(ordinary_api, "/works", registration).status_code == 201
    route = f"/works/{manifest['work']['id']}"
    preview_response = post(ordinary_api, f"{route}/preview", {})
    assert preview_response.status_code == 200
    [preview] = preview_response.json()["expanded"]
    assert preview["target"]["options"]["publish_mode"] == "public"

    request = {"retry_policy": {"max_attempts": 3, "retry_delays_seconds": [0, 0]}}
    first_key = uid()
    response = post(ordinary_api, f"{route}/submit", request, first_key)
    assert response.status_code == 201
    [created] = response.json()["tasks"]
    assert created["created"] and "b18_run_id" not in created["snapshot"]
    assert created["snapshot"]["target"] == preview["target"]
    task_id = created["task_id"]

    platform["calls"].clear()
    platform["behavior"] = "drop_response"
    platform["archives_pending"] = True
    store = ExecutionStore(service.database)
    store.register()
    Supervisor(store).execute(store.claim())
    uncertain = service.task(task_id)
    assert uncertain["status"] == "UNCERTAIN" and uncertain["receipt"] is None
    assert uncertain["attempt_count"] == 1 and uncertain["attempt"]["exit_confirmed"]
    assert uncertain["attempt"]["result"]["submission_state"] == "unknown"
    [automatic_query] = ordinary_api.get(f"/api/v1/tasks/{task_id}/queries").json()["items"]
    assert automatic_query["status"] == "FINISHED"
    assert automatic_query["error"] == "query_inconclusive"
    [posted] = platform["submissions"]
    assert posted["is_only_self"] == 0 and posted["space_hidden"] == 2

    for replay_key in (first_key, uid()):
        replay = post(ordinary_api, f"{route}/submit", request, replay_key)
        assert replay.status_code == 201
        assert [task["task_id"] for task in replay.json()["tasks"]] == [task_id]
    restarted_store = ExecutionStore(service.database)
    restarted_store.register()
    assert restarted_store.claim() is None

    platform["archives_pending"] = False
    response = post(ordinary_api, f"/tasks/{task_id}/query", {})
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["error"] is None and result["result"]["query_verified"]
    assert result["result"]["video_keys"] == [video["filename"] for video in posted["videos"]]
    [final_task] = ordinary_api.get("/api/v1/tasks").json()["items"]
    assert final_task["id"] == task_id and final_task["status"] == "SUCCEEDED"
    final_task = service.task(task_id)
    assert final_task["attempt_count"] == 1 and final_task["attempt"]["status"] == "UNCERTAIN"
    assert final_task["receipt"]["source"] == "bilibili-query"
    paths = [urlsplit(call).path for call in platform["calls"]]
    assert paths.count("/x/web/archives") == 2
    add_paths = [path for path in paths if "/add" in path]
    assert add_paths == ["/x/vu/web/add/v3"]
    assert len(platform["submissions"]) == 1 and restarted_store.claim() is None
    (tmp_path / "response-loss-evidence.json").write_text(
        json.dumps(
            {
                "status_after_disconnect": uncertain["status"],
                "status_after_readback": final_task["status"],
                "tasks": 1,
                "attempts": final_task["attempt_count"],
                "original_attempt_status": final_task["attempt"]["status"],
                "automatic_query_error": automatic_query["error"],
                "archive_list_queries": paths.count("/x/web/archives"),
                "add_paths": add_paths,
                "mock_acceptances": len(platform["submissions"]),
                "receipt_source": final_task["receipt"]["source"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
