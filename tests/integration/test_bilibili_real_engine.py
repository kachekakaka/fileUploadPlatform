"""Cross-repository contract test: real Rust, real Core, local HTTP only."""

import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import queue
import shutil
import subprocess
from threading import Event
import time
from urllib.parse import urlsplit

import pytest
from fastapi.testclient import TestClient

from conftest import save_manifest
from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.api.app import create_app
from upload_platform.application.accounts import Accounts, MaintenanceRequest, ProcessIdentity
from upload_platform.application.contracts import (
    Registration,
    RetryPolicy,
    ScheduleRequest,
    Submission,
)
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, uid, utc
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure.database import Database
from upload_platform.infrastructure.windows import JobHost
from upload_platform.scheduling.runtime import ScheduleService, Scheduler


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
    capability_file.write_text(json.dumps({**capabilities, "verified_hide_in_space": True}))
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
    def create(*, single=False, parent=None, **options):
        directory, manifest = work_factory(directory=parent / uid() if parent else None)
        manifest["work"].pop("cover", None)
        if single:
            manifest["upload_units"][0].update(mode="independent", videos=["v1"])
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
    "single, second_part_failure, hide, mode",
    [
        (True, False, False, "public"),
        (True, False, True, "public"),
        (True, False, False, "self_only"),
        (True, False, True, "self_only"),
        (False, False, False, "public"),
        (False, False, True, "public"),
        (False, False, False, "self_only"),
        (False, False, True, "self_only"),
        (False, True, False, "public"),
    ],
    ids=[
        "single-public",
        "single-hidden",
        "single-private",
        "single-private-hidden",
        "multi-public",
        "multi-hidden",
        "multi-private",
        "multi-private-hidden",
        "p2-complete",
    ],
)
def test_real_core_and_rust_login_upload_query_contract(
    real_bilibili, bilibili_work, monkeypatch, single, second_part_failure, hide, mode
):
    service, server = real_bilibili
    config = service.database.config
    deployment = replace(
        config.bilibili, allow_self_only=mode == "self_only", allow_hide_in_space=hide
    )
    capabilities = deployment.capabilities()
    capabilities.update(verified_publish_modes=["public"], verified_hide_in_space=False)
    deployment.capabilities_file.write_text(json.dumps(capabilities), encoding="utf-8")
    monkeypatch.setattr(service.database, "config", replace(config, bilibili=deployment))

    directory, manifest = bilibili_work(single=single, publish_mode=mode, hide_in_space=hide)
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
    assert [part["title"] for part in posted["videos"]] == (
        ["第一 P"] if single else ["第一 P", "第二 P"]
    )
    assert task["snapshot"]["target"]["options"]["publish_mode"] == mode
    assert task["snapshot"]["target"]["options"].get("hide_in_space", False) is hide
    assert server[2] not in json.dumps(service.task(task_id))


@pytest.mark.parametrize(
    ("options", "permissions", "disabled", "error_code"),
    [
        (
            {"publish_mode": "self_only", "hide_in_space": True},
            {"allow_self_only": True, "allow_hide_in_space": True},
            {"upload_ready": False},
            "UNSUPPORTED_OPTION",
        ),
        (
            {"publish_mode": "self_only"},
            {},
            {"verified_publish_modes": ["public"]},
            "UNSUPPORTED_OPTION",
        ),
        (
            {"publish_mode": "self_only"},
            {"allow_self_only": True},
            {"supports_self_only": False},
            "UNSUPPORTED_OPTION",
        ),
        (
            {"hide_in_space": True},
            {"allow_hide_in_space": True},
            {"supports_hide_in_space": False},
            "UNSUPPORTED_OPTION",
        ),
        ({"hide_in_space": True}, {}, {"verified_hide_in_space": False}, "UNSUPPORTED_OPTION"),
        ({}, {}, {"engine_build_id": "different-fixture"}, "engine_incompatible"),
    ],
    ids=[
        "uploads-closed",
        "mode-not-verified",
        "private-unsupported",
        "hide-unsupported",
        "hide-not-verified",
        "wrong-build",
    ],
)
def test_ordinary_posting_checks_capabilities_at_each_stage(
    real_bilibili,
    ordinary_api,
    bilibili_work,
    monkeypatch,
    options,
    permissions,
    disabled,
    error_code,
):
    service, (_, platform, _) = real_bilibili
    database = service.database
    config = database.config
    deployment = replace(config.bilibili, **permissions)
    monkeypatch.setattr(database, "config", replace(config, bilibili=deployment))
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


@pytest.mark.parametrize("single", [True, False], ids=["scheduled-single", "periodic-multi"])
def test_public_release_scope_preserves_scheduled_ledger_and_part_order(
    real_bilibili, ordinary_api, bilibili_work, tmp_path, clock, monkeypatch, single
):
    service, (_, platform, _) = real_bilibili
    deployment = service.database.config.bilibili
    caps = deployment.capabilities()
    assert caps["supports_hide_in_space"] and caps["supports_self_only"]
    caps["verified_publish_modes"] = ["public"]
    caps.pop("verified_hide_in_space")
    deployment.capabilities_file.write_text(json.dumps(caps), encoding="utf-8")
    with pytest.raises(Problem, match="测试引擎"):
        replace(deployment, allow_test_engine=False)
    monkeypatch.setattr(service, "clock", clock)
    parent = tmp_path / "public-works"
    for denied in ({"publish_mode": "self_only"}, {"hide_in_space": True}):
        directory, manifest = bilibili_work(single=single, **denied)
        response = post(ordinary_api, "/works", {"directory": str(directory), "manifest": manifest})
        assert response.status_code == 409 and response.json()["code"] == "UNSUPPORTED_OPTION"
    assert not platform["submissions"]

    directory, manifest = bilibili_work(single=single, parent=parent)
    work_id = manifest["work"]["id"]
    assert (
        post(
            ordinary_api, "/works", {"directory": str(directory), "manifest": manifest}
        ).status_code
        == 201
    )
    route = f"/works/{work_id}"
    [preview] = post(ordinary_api, f"{route}/preview", {}).json()["expanded"]
    store = ExecutionStore(service.database, clock=clock)
    store.register()
    if single:
        response = post(
            ordinary_api,
            f"{route}/submit",
            {
                "execution_type": "scheduled",
                "scheduled_at": utc(clock() + 2),
            },
        )
        assert response.status_code == 201
        assert store.claim() is None
    else:
        ScheduleService(service).create(
            uid(),
            ScheduleRequest(
                parent_directory=str(parent),
                kind="daily",
                local_time="00:00:01",
                timezone="UTC",
            ),
        )
    clock.value += 2
    if not single:
        scheduler = Scheduler(service, "public-periodic-test")
        scheduler.tick()
        scheduler.tick()
    [scheduled] = ordinary_api.get("/api/v1/tasks").json()["items"]
    response = post(ordinary_api, f"{route}/submit", {})
    assert response.status_code == 201
    [reused] = response.json()["tasks"]
    assert reused["task_id"] == scheduled["id"] and not reused["created"]
    assert reused["snapshot"]["target"] == preview["target"]
    assert "b18_run_id" not in reused["snapshot"]
    Supervisor(store).execute(store.claim())
    result = service.task(reused["task_id"])
    assert result["status"] == "SUCCEEDED" and result["receipt"]["evidence"]["query_verified"]
    [posted] = platform["submissions"]
    assert (posted["is_only_self"], posted["space_hidden"]) == (0, 2)
    assert [video["title"] for video in posted["videos"]] == [
        part["title"] for part in preview["parts"]
    ]
    assert store.claim() is None


@pytest.mark.parametrize(
    "fault",
    [
        "drop_response",
        "response_timeout",
        "unwritable_result",
        "multiple_matches",
        "incomplete_pages",
    ],
)
def test_ordinary_posting_response_loss_recovers_without_reposting(
    real_bilibili, ordinary_api, bilibili_work, tmp_path, fault
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
    platform["behavior"] = "normal" if fault == "unwritable_result" else "drop_response"
    platform["archives_pending"] = True
    archive_pages = 1
    if fault in ("multiple_matches", "incomplete_pages"):
        platform["archives_pending"] = False
        platform["archive_ids"] = [123, 124]
    if fault == "incomplete_pages":
        platform["archive_page_size"] = 1
        platform["archive_ids"] = [123, 124, 125, 126]
        archive_pages = 3
    store = ExecutionStore(service.database)
    store.register()
    attempt = store.claim()
    execution = service.database.config.data_dir / "executions" / attempt["id"]
    if fault == "unwritable_result":

        def block_result(point):
            if point == "after_handoff":
                (execution / "result.json").mkdir()

        Supervisor(store, block_result).execute(attempt)
        assert (execution / "result.json").is_dir()
        assert (execution / "event-000005.json").is_file()  # Accepted response was persisted.
    elif fault == "response_timeout":
        arrived, release = Event(), Event()
        platform.update(behavior=fault, submit_arrived=arrived, release_submit=release)
        start = time.monotonic()
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(Supervisor(store).execute, attempt)
            try:
                assert arrived.wait(15)
                initial = service.task(task_id)["attempt"]
                latest = initial
                deadline = time.monotonic() + 10
                while latest["heartbeat_at"] <= initial["heartbeat_at"] + 2:
                    assert not pending.done() and time.monotonic() < deadline
                    time.sleep(0.05)
                    latest = service.task(task_id)["attempt"]
                assert latest["status"] == "RUNNING"
                pending.result(timeout=75)
                assert time.monotonic() - start >= 59
            finally:
                release.set()
    else:
        Supervisor(store).execute(attempt)
    uncertain = service.task(task_id)
    assert uncertain["status"] == "UNCERTAIN" and uncertain["receipt"] is None
    assert uncertain["attempt_count"] == 1 and uncertain["attempt"]["exit_confirmed"]
    if fault == "unwritable_result":
        assert uncertain["attempt"]["result"] is None
    else:
        assert uncertain["attempt"]["result"]["submission_state"] == "unknown"
    [automatic_query] = ordinary_api.get(f"/api/v1/tasks/{task_id}/queries").json()["items"]
    assert automatic_query["status"] == "FINISHED"
    assert automatic_query["error"] == "query_inconclusive"
    query_path = (
        service.database.config.data_dir / "queries" / automatic_query["id"] / "result.json"
    )
    expected_query_error = {
        "multiple_matches": "QUERY_MULTIPLE_MATCHES",
        "incomplete_pages": "QUERY_INCOMPLETE",
    }.get(fault, "QUERY_NO_MATCH")
    assert json.loads(query_path.read_text())["error"] == {"code": expected_query_error}
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
    platform["archive_ids"] = [123]
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
    assert paths.count("/x/web/archives") == archive_pages + 1
    add_paths = [path for path in paths if "/add" in path]
    assert add_paths == ["/x/vu/web/add/v3"]
    assert len(platform["submissions"]) == 1 and restarted_store.claim() is None
    (tmp_path / "response-loss-evidence.json").write_text(
        json.dumps(
            {
                "status_after_disconnect": uncertain["status"],
                "fault": fault,
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


@pytest.mark.parametrize(("disconnects", "budget"), [(1, 2), (99, 2), (99, 1)])
def test_three_part_p2_disconnect_only_retries_before_submission(
    real_bilibili, ordinary_api, bilibili_work, disconnects, budget
):
    service, (_, receiver, _) = real_bilibili
    directory, manifest = bilibili_work(publish_mode="self_only")
    (directory / "third.mp4").write_bytes(b"distinct third part")
    manifest["assets"]["videos"].append(
        {"id": "v3", "path": "third.mp4", "metadata": {"title": "第三 P"}}
    )
    manifest["upload_units"][0]["videos"].append("v3")
    save_manifest(directory, manifest)
    assert (
        post(
            ordinary_api, "/works", {"directory": str(directory), "manifest": manifest}
        ).status_code
        == 201
    )
    route = f"/works/{manifest['work']['id']}/submit"
    response = post(
        ordinary_api,
        route,
        {
            "retry_policy": {
                "max_attempts": budget,
                "retry_delays_seconds": [0] * (budget - 1),
            }
        },
    )
    assert response.status_code == 201, response.text
    task_id = response.json()["tasks"][0]["task_id"]

    class Disconnect(dict):
        def __getitem__(self, name):
            if name == "status":
                # The fixture has read this chunk. Drop that socket before writing any reply.
                raise ConnectionResetError("expected loopback P2 interruption")
            return super().__getitem__(name)

    fault = Disconnect(path="/bucket/key2.mp4", step="chunk", remaining=disconnects)
    receiver["bucket_fault"] = fault
    store = ExecutionStore(service.database)
    store.register()
    first = store.claim()
    assert first["task_id"] == task_id
    Supervisor(store).execute(first)
    current = service.task(task_id)
    assert fault["remaining"] < disconnects
    if disconnects > 1:
        assert not receiver["submissions"]
        assert current["attempt_count"] == 1
        assert current["attempt"]["exit_confirmed"]
        assert current["attempt"]["result"]["submission_state"] == "not_submitted"
        if budget == 1:
            assert current["status"] == "FAILED" and store.claim() is None
            return
        second = store.claim()
        assert second["task_id"] == task_id and second["id"] != first["id"]
        Supervisor(store).execute(second)
        current = service.task(task_id)
    assert current["status"] == "SUCCEEDED"
    assert current["attempt_count"] == (1 if disconnects == 1 else 2)
    [posted] = receiver["submissions"]
    assert posted["is_only_self"] == 1
    assert [video["title"] for video in posted["videos"]] == ["第一 P", "第二 P", "第三 P"]
    assert store.claim() is None


def test_version_mismatch_cannot_replace_active_engine_or_start_old_snapshot(
    real_bilibili, bilibili_work, tmp_path
):
    service, (_, platform, _) = real_bilibili
    database = service.database
    current = database.config.bilibili
    tasks = []
    for _ in range(2):
        directory, manifest = bilibili_work(single=True)
        service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
        [task] = service.submit(manifest["work"]["id"], uid(), Submission())[1]["tasks"]
        tasks.append(task["task_id"])

    other_directory = tmp_path / "other-build"
    other_directory.mkdir()
    other_exe = other_directory / current.executable.name
    shutil.copyfile(current.executable, other_exe)
    other_caps = other_directory / "capabilities.json"
    other_build = "other-release-fixture"
    other_caps.write_text(json.dumps({**current.capabilities(), "engine_build_id": other_build}))
    other = Database(
        replace(
            database.config,
            bilibili=replace(
                current,
                executable=other_exe,
                build_id=other_build,
                capabilities_file=other_caps,
            ),
        )
    )
    arrived, release = Event(), Event()
    platform.update(behavior="hold_response", submit_arrived=arrived, release_submit=release)
    store = ExecutionStore(database)
    store.register()
    first = store.claim()
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(Supervisor(store).execute, first)
        try:
            assert arrived.wait(15)
            assert len(platform["submissions"]) == 1
            switch = ExecutionStore(other)
            switch.register()
            second = switch.claim()
            assert {first["task_id"], second["task_id"]} == set(tasks)
            calls_before = len(platform["calls"])
            Supervisor(switch).execute(second)
            blocked = service.task(second["task_id"])
            assert blocked["status"] == "FAILED"
            assert blocked["attempt"]["error"] == "engine_incompatible"
            assert blocked["attempt"]["handed_off_at"] is None
            assert len(platform["calls"]) == calls_before
            active = service.task(first["task_id"])
            assert active["status"] == "RUNNING"
            assert active["attempt"]["execution"]["engine_executable"] == str(current.executable)
        finally:
            release.set()
            other.close()
        pending.result(timeout=15)
    assert service.task(first["task_id"])["status"] == "SUCCEEDED"
    assert len(platform["submissions"]) == 1


def test_worker_renews_lease_while_rust_waits_for_submit_lock(real_bilibili, bilibili_work):
    import msvcrt

    service, (_, platform, _) = real_bilibili
    database = service.database
    directory, manifest = bilibili_work(single=True)
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    [submitted] = service.submit(manifest["work"]["id"], uid(), Submission())[1]["tasks"]
    task_id = submitted["task_id"]
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    lock = database.config.bilibili.state_root / "locks" / "bilibili-42-submit.lock"
    with lock.open("a+b") as held, ThreadPoolExecutor(max_workers=1) as pool:
        held.seek(0)
        msvcrt.locking(held.fileno(), msvcrt.LK_NBLCK, 1)
        pending = pool.submit(Supervisor(store).execute, attempt)
        try:
            deadline = time.monotonic() + 15
            started = service.task(task_id)["attempt"]
            while started["phase"] != "waiting_submit":
                assert not pending.done() and time.monotonic() < deadline
                time.sleep(0.05)
                started = service.task(task_id)["attempt"]
            latest = started
            deadline = time.monotonic() + 10
            while latest["heartbeat_at"] <= started["heartbeat_at"] + database.config.lease_seconds:
                assert not pending.done() and time.monotonic() < deadline
                time.sleep(0.05)
                latest = service.task(task_id)["attempt"]
            assert latest["phase"] == "waiting_submit" and platform["submissions"] == []
        finally:
            msvcrt.locking(held.fileno(), msvcrt.LK_UNLCK, 1)
        pending.result(timeout=15)
    assert service.task(task_id)["status"] == "SUCCEEDED"
    assert len(platform["submissions"]) == 1
