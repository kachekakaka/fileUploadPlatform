"""Cross-repository contract test: real Rust, real Core, local HTTP only."""

import importlib.util
import json
import os
from dataclasses import replace
from pathlib import Path
import queue
import subprocess
import time

import pytest

from conftest import save_manifest
from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.application.accounts import Accounts, MaintenanceRequest, ProcessIdentity
from upload_platform.application.contracts import Registration, RetryPolicy, Submission
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.application.service import Service
from upload_platform.domain.core import uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure.database import Database
from upload_platform.infrastructure.windows import JobHost


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
@pytest.mark.skipif(
    not os.getenv("BUE_TEST_EXECUTABLE") or not os.getenv("BUE_HTTP_TEST_MODULE"),
    reason="requires separately built fixture Rust engine and its HTTP fixture module",
)
def test_real_core_and_rust_login_upload_query_contract(
    config, tmp_path, work_factory, monkeypatch, second_part_failure, hide, mode
):
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
    database.initialize()
    service = Service(database)
    accounts = Accounts(service)
    try:
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
        directory, manifest = work_factory()
        manifest["work"].pop("cover", None)
        for video in manifest["assets"]["videos"]:
            video.pop("subtitles", None)
        manifest["upload_units"][0]["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:42",
                "options": {"category_id": 171, "copyright": 1, "publish_mode": mode},
            }
        ]
        options = manifest["upload_units"][0]["targets"][0]["options"]
        options["hide_in_space"] = hide
        save_manifest(directory, manifest)
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
        task_id = service.submit(manifest["work"]["id"], uid(), submission)[1]["tasks"][0][
            "task_id"
        ]
        store = ExecutionStore(database)
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
        assert len(server[1]["submissions"]) == 1
        [posted] = server[1]["submissions"]
        assert posted["space_hidden"] == (1 if hide else 2)
        assert posted["is_only_self"] == (1 if mode == "self_only" else 0)
        assert task["snapshot"]["target"]["options"].get("hide_in_space", False) is hide
        assert server[2] not in json.dumps(service.task(task_id))
    finally:
        database.close()
        fixture.close()
