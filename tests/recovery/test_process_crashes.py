from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import pytest
from sqlalchemy import select

from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, now
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import process_creation, stop_verified


def env_for(config):
    return {
        **os.environ,
        "PYTHONUTF8": "1",
        "UPLOAD_PLATFORM_DATA_DIR": str(config.data_dir),
        "UPLOAD_PLATFORM_HEARTBEAT_SECONDS": "0.3",
        "UPLOAD_PLATFORM_LEASE_SECONDS": "2",
        "UPLOAD_PLATFORM_POLL_SECONDS": "0.05",
    }


def wait_until(fn, timeout=15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.04)
    raise AssertionError("process condition timed out")


def cleanup_test_launch(marker):
    """This proof is a test fixture, deliberately unavailable to product recovery."""
    import win32api
    import win32process
    import win32event
    import win32job

    evidence = json.loads(marker.read_text(encoding="utf-8"))
    if "pid" not in evidence:
        return
    process = win32api.OpenProcess(0x00100000 | 0x1000 | 0x0001, False, evidence["pid"])
    try:
        assert process_creation(process) == evidence["creation_time"]
        if evidence["point"] == "after_assign":
            job = win32job.OpenJobObject(0x0004 | 0x0008, False, evidence["execution"]["job_name"])
            try:
                assert win32job.IsProcessInJob(process, job)
                win32job.TerminateJobObject(job, 92)
                assert win32event.WaitForSingleObject(process, 5000) == win32event.WAIT_OBJECT_0
                wait_until(
                    lambda: (
                        win32job.QueryInformationJobObject(
                            job, win32job.JobObjectBasicAccountingInformation
                        )["ActiveProcesses"]
                        == 0
                    )
                )
            finally:
                job.Close()
        else:
            win32process.TerminateProcess(process, 92)
            assert win32event.WaitForSingleObject(process, 5000) == win32event.WAIT_OBJECT_0
    finally:
        process.Close()


@pytest.mark.parametrize(
    "point,expected",
    [
        ("after_intent", "RECOVERY_PENDING"),
        ("after_create", "RECOVERY_PENDING"),
        ("after_assign", "RECOVERY_PENDING"),
        ("after_identity", "RETRY_WAIT"),
        ("after_permission", "UNCERTAIN"),
        ("after_result_before_commit", "SUCCEEDED"),
    ],
)
def test_killed_worker_launch_windows(database, submit_work, tmp_path, point, expected):
    _, _, tasks = submit_work()
    submit_work()  # another task must not receive the first task's occupied slot early
    service = Service(database)
    marker = tmp_path / "crash-marker.json"
    helper = Path(__file__).resolve().parents[1] / "helpers" / "worker_once.py"
    log = open(tmp_path / "worker-crash.log", "w", encoding="utf-8")
    process = subprocess.Popen(
        [sys.executable, str(helper), point, str(marker)],
        env=env_for(database.config),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    try:
        assert process.wait(timeout=25) == 91
        evidence = json.loads(marker.read_text(encoding="utf-8"))
        with database.read() as conn:
            attempt = db.get(conn, db.attempts, evidence["attempt_id"])
        if point == "after_result_before_commit":
            assert attempt["exit_confirmed"]
            assert attempt["execution"]["exit_proof"] == "job_empty_and_host_exited"
            # Confirmation is durable, but no completion/receipt/slot release happened yet.
            assert service.task(attempt["task_id"])["status"] == "RUNNING"
            assert service.task(attempt["task_id"])["receipt"] is None
        store = ExecutionStore(database)
        store.register()
        assert store.claim() is None
        wait_until(
            lambda: (
                now()
                > service.task(attempt["task_id"])["attempt"]["heartbeat_at"]
                + database.config.lease_seconds
                + 0.1
            )
        )
        recovery = store.recovery_claim()
        assert recovery is not None
        Supervisor(store).recover(recovery)
        task = service.task(attempt["task_id"])
        assert task["status"] == expected, task
        if expected == "RECOVERY_PENDING":
            assert store.claim() is None
            assert not task["attempt"]["exit_confirmed"]
        else:
            assert task["attempt"]["exit_confirmed"]
            assert store.claim() is not None
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=5)
        log.close()
        if marker.exists() and point in ("after_create", "after_assign"):
            cleanup_test_launch(marker)
        with database.read() as conn:
            attempts = db.many(conn, select(db.attempts))
        for attempt in attempts:
            if attempt["execution"].get("assigned"):
                stop_verified(attempt["execution"])


def test_killed_worker_child_tree_blocks_account_until_verified_exit(
    database, submit_work, tmp_path
):
    import win32api
    import win32event

    _, _, tasks = submit_work(scenario="child")
    service = Service(database)
    task_id = tasks[0]["task_id"]
    log = open(tmp_path / "worker.log", "w", encoding="utf-8")
    worker = subprocess.Popen(
        [sys.executable, "-m", "upload_platform", "worker"],
        env=env_for(database.config),
        stdout=log,
        stderr=subprocess.STDOUT,
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    child = None
    try:

        def get_child():
            rows = service.event_page(task_id)["items"]
            return next(
                (row["payload"]["child_pid"] for row in rows if row["payload"].get("child_pid")),
                None,
            )

        child_id = wait_until(get_child, 25)
        child = win32api.OpenProcess(0x00100000 | 0x1000, False, child_id)
        worker.terminate()  # only the test-owned worker; the inherited Job keeps host/child alive
        worker.wait(timeout=5)
        submit_work()
        store = ExecutionStore(database)
        store.register()
        assert store.claim() is None
        assert win32event.WaitForSingleObject(child, 0) != win32event.WAIT_OBJECT_0
        wait_until(
            lambda: (
                now()
                > service.task(task_id)["attempt"]["heartbeat_at"]
                + database.config.lease_seconds
                + 0.1
            )
        )
        recovery = store.recovery_claim()
        Supervisor(store).recover(recovery)
        assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
        assert service.task(task_id)["status"] == "UNCERTAIN"
        assert store.claim() is not None
    finally:
        if worker.poll() is None:
            worker.terminate()
            worker.wait(timeout=5)
        log.close()
        task = service.task(task_id)
        if task["attempt"] and task["attempt"]["execution"]:
            stop_verified(task["attempt"]["execution"])
        if child:
            child.Close()


def test_identity_mismatch_does_not_kill_other_process_or_release_slot(
    database, submit_work, tmp_path
):
    _, _, tasks = submit_work()
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    sleeper = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(60)"],
        creationflags=subprocess.CREATE_NO_WINDOW,
    )
    from upload_platform.infrastructure.windows import boot_id

    try:
        assert store.starting(
            attempt,
            {
                "boot_id": boot_id(),
                "pid": sleeper.pid,
                "creation_time": "intentionally-wrong",
                "assigned": True,
                "job_name": "Local\\UploadPlatform-test-missing",
            },
        )
        time.sleep(database.config.lease_seconds + 0.1)
        recovery_store = ExecutionStore(database)
        recovery_store.register()
        recovery = recovery_store.recovery_claim()
        Supervisor(recovery_store).recover(recovery)
        assert sleeper.poll() is None
        task = service.task(tasks[0]["task_id"])
        assert (
            task["status"] == "RECOVERY_PENDING"
            and task["attempt"]["error"] == "process_identity_mismatch"
        )
        assert recovery_store.claim() is None
    finally:
        sleeper.terminate()
        sleeper.wait(timeout=5)


def test_two_recovery_owners_only_one_wins_and_old_generation_rejected(database, submit_work):
    submit_work()
    owner = ExecutionStore(database)
    owner.register()
    original = owner.claim()
    time.sleep(database.config.lease_seconds + 0.1)
    competitors = [ExecutionStore(database), ExecutionStore(database)]
    for store in competitors:
        store.register()
    with ThreadPoolExecutor(2) as pool:
        claims = list(pool.map(lambda store: store.recovery_claim(), competitors))
    assert sum(claim is not None for claim in claims) == 1
    with pytest.raises(Problem):
        owner.heartbeat(original)
    winner = next(store for store, claim in zip(competitors, claims) if claim)
    claim = next(c for c in claims if c)
    Supervisor(winner).recover(claim)
    with pytest.raises(Problem):
        winner.finish(claim, "retryable", proof="duplicate", recovery=True)
