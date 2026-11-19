import json
from pathlib import Path
import subprocess
import sys

import pytest
from sqlalchemy import select

from upload_platform.domain.core import now
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import stop_verified
from test_bilibili_runtime import setup_bilibili as setup_bilibili
from test_process_crashes import env_for, wait_until, cleanup_test_launch


@pytest.mark.parametrize(
    "point,expected",
    [
        ("after_intent", "RECOVERY_PENDING"),
        ("after_create", "RECOVERY_PENDING"),
        ("after_assign", "RECOVERY_PENDING"),
        ("after_identity", "RETRY_WAIT"),
        ("after_handoff", "UNCERTAIN"),
        ("during_bilibili_send", "UNCERTAIN"),
        ("after_bilibili_send", "UNCERTAIN"),
        ("after_result_before_commit", "SUCCEEDED"),
    ],
)
def test_actual_worker_death_at_bilibili_boundaries(setup_bilibili, tmp_path, point, expected):
    import win32job

    database, service, _, task_id, control = setup_bilibili
    deployment = database.config.bilibili
    config_file = tmp_path / "deployment.json"
    config_file.write_text(
        json.dumps(
            {
                "executable": str(deployment.executable),
                "build_id": deployment.build_id,
                "state_root": str(deployment.state_root),
                "capabilities_file": str(deployment.capabilities_file),
                "allow_test_engine": True,
                "test_arguments": list(deployment.test_arguments),
            }
        )
    )
    marker, gate = tmp_path / "crash.json", tmp_path / "release-crash"
    if point in ("during_bilibili_send", "after_bilibili_send"):
        control.write_text(json.dumps({"gate": str(tmp_path / "hold-engine-before-submit")}))
    helper = Path(__file__).parents[1] / "helpers/worker_once.py"
    environment = {
        **env_for(database.config),
        "UPLOAD_PLATFORM_BILIBILI_CONFIG": str(config_file),
        "UPLOAD_PLATFORM_TEST_CRASH_GATE": str(gate),
        "BUE_FIXTURE_CONTROL": str(control),
    }
    job = None
    with (tmp_path / "worker.log").open("w", encoding="utf-8") as log:
        process = subprocess.Popen(
            [sys.executable, str(helper), point, str(marker)],
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:
            wait_until(marker.exists)
            evidence = json.loads(marker.read_text())
            with database.read() as conn:
                attempt = db.get(conn, db.attempts, evidence["attempt_id"])
            if attempt["execution"].get("assigned"):
                job = win32job.OpenJobObject(
                    0x0004 | 0x0008, False, attempt["execution"]["job_name"]
                )
            if point in ("during_bilibili_send", "after_bilibili_send"):
                received = Path(attempt["execution"]["result_path"]).with_name(
                    "fixture-received.json"
                )
                if point == "after_bilibili_send":
                    wait_until(received.is_file)
                    assert json.loads(received.read_text())["identity"]["task_id"] == task_id
                else:
                    assert not received.exists()
            gate.touch()
            assert process.wait(timeout=10) == 91
            store = ExecutionStore(database)
            store.register()
            wait_until(
                lambda: (
                    now()
                    > service.task(task_id)["attempt"]["heartbeat_at"]
                    + database.config.lease_seconds
                    + 0.1
                )
            )
            Supervisor(store).recover(store.recovery_claim())
            task = service.task(task_id)
            assert task["status"] == expected, task
            if point in ("during_bilibili_send", "after_bilibili_send"):
                assert task["attempt_count"] == 1 and task["receipt"] is None
                assert store.claim() is None
            with database.read() as conn:
                leases = db.many(conn, select(db.leases).where(db.leases.c.released_at.is_(None)))
                receipts = db.many(conn, select(db.receipts))
            assert bool(leases) == (expected == "RECOVERY_PENDING")
            assert len(receipts) == (1 if expected == "SUCCEEDED" else 0)
            if expected == "SUCCEEDED":
                assert len(service.event_page(task_id)["items"]) == len(
                    {x["id"] for x in service.event_page(task_id)["items"]}
                )
        finally:
            gate.touch()
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
            if marker.exists() and point in ("after_create", "after_assign"):
                cleanup_test_launch(marker)
            task = service.task(task_id)
            if task["attempt"] and task["attempt"]["execution"].get("assigned"):
                stop_verified(task["attempt"]["execution"])
            if job:
                job.Close()
