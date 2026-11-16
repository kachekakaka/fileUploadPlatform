from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
import json
import subprocess
import sys
import threading

import pytest
from sqlalchemy import select
import win32api
import win32event
import win32process

from upload_platform.application.contracts import Empty, Submission
from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, now, uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.execution.protocol import HostEvent, identity
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import write_json
from upload_platform.infrastructure.windows import JobHost, boot_id
from test_process_crashes import env_for, wait_until


def test_host_exited_job_missing_keeps_live_child_and_slot(database, submit_work, tmp_path):
    task_id = submit_work(scenario="child")[2][0]["task_id"]
    service = Service(database)
    helper = Path(__file__).resolve().parents[1] / "helpers" / "worker_once.py"
    child = host_process = None
    with open(tmp_path / "worker.log", "w", encoding="utf-8") as log:
        worker = subprocess.Popen(
            [sys.executable, str(helper), "before_tree_verification", str(tmp_path / "crash.json")],
            env=env_for(database.config),
            stdout=log,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        try:

            def child_pid():
                return next(
                    (
                        e["payload"].get("child_pid")
                        for e in service.event_page(task_id)["items"]
                        if e["payload"].get("child_pid")
                    ),
                    None,
                )

            child = win32api.OpenProcess(0x00100000 | 0x1000 | 1, False, wait_until(child_pid, 25))
            execution = service.task(task_id)["attempt"]["execution"]
            host_process = win32api.OpenProcess(0x00100000, False, execution["pid"])
            service.cancel(task_id, uid(), Empty())
            assert worker.wait(timeout=15) == 91
            assert win32event.WaitForSingleObject(host_process, 0) == win32event.WAIT_OBJECT_0
            submit_work()
            recovery = ExecutionStore(database)
            recovery.register()
            wait_until(
                lambda: (
                    now()
                    > service.task(task_id)["attempt"]["heartbeat_at"]
                    + database.config.lease_seconds
                )
            )
            attempt = recovery.recovery_claim()
            assert attempt is not None
            Supervisor(recovery).recover(attempt)
            assert win32event.WaitForSingleObject(child, 0) == win32event.WAIT_TIMEOUT
            task = service.task(task_id)
            assert task["status"] == "RECOVERY_PENDING"
            assert not task["attempt"]["exit_confirmed"]
            assert task["attempt"]["error"] == "job_exit_unconfirmed"
            assert recovery.claim() is None
            assert any(
                e["type"] == "recovery.blocked" for e in service.event_page(task_id)["items"]
            )
        finally:
            if worker.poll() is None:
                worker.terminate()
                worker.wait(timeout=5)
            if child:
                if win32event.WaitForSingleObject(child, 0) == win32event.WAIT_TIMEOUT:
                    # Test-owned process handle, captured from this execution's actual event.
                    win32process.TerminateProcess(child, 92)
                    assert win32event.WaitForSingleObject(child, 5000) == win32event.WAIT_OBJECT_0
                child.Close()
            if host_process:
                host_process.Close()


@pytest.mark.parametrize("order", ["cancel_first", "finish_first", "race"])
def test_real_success_commit_competes_with_cancel(database, submit_work, order):
    task_id = submit_work()[2][0]["task_id"]
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    ready, release = threading.Event(), threading.Event()
    race = threading.Barrier(2)

    def pause(point):
        if point == "after_result_before_commit":
            ready.set()
            assert release.wait(10)
            if order == "race":
                race.wait(timeout=10)

    def cancel():
        if order == "race":
            race.wait(timeout=10)
        return service.cancel(task_id, uid(), Empty())

    with ThreadPoolExecutor(2) as pool:
        execution = pool.submit(Supervisor(store, pause).execute, attempt)
        try:
            assert ready.wait(10)
            if order == "finish_first":
                release.set()
                execution.result(timeout=10)
                cancelled = pool.submit(cancel)
            else:
                cancelled = pool.submit(cancel)
                if order == "cancel_first":
                    assert cancelled.result(timeout=5)[1]["status"] == "RUNNING"
                release.set()
            cancelled.result(timeout=10)
            execution.result(timeout=10)
        finally:
            release.set()
    task = service.task(task_id)
    assert task["status"] == task["attempt"]["status"] == "SUCCEEDED"
    assert task["attempt"]["exit_confirmed"] and task["receipt"]["fake"]
    service.cancel(task_id, uid(), Empty())
    events = service.event_page(task_id)["items"]
    assert sum(e["type"] == "task.succeeded" for e in events) == 1
    assert not any(e["type"] == "task.cancelled" for e in events)
    assert sum(e["type"] == "task.cancel_not_effective" for e in events) == int(
        task["cancel_requested_at"] is not None
    )
    with database.read() as conn:
        assert len(db.many(conn, select(db.receipts))) == 1
        assert len(db.many(conn, select(db.attempts))) == 1
        assert all(lease["released_at"] is not None for lease in db.many(conn, select(db.leases)))


@pytest.mark.parametrize("invalid_generation", [False, True])
def test_real_late_result_after_recovery_takeover(
    database, clock, submit_work, tmp_path, invalid_generation
):
    _, manifest, tasks = submit_work(scenario="late_result", duration=0.4)
    task_id = tasks[0]["task_id"]
    service = Service(database, clock)
    owner = ExecutionStore(database, clock=clock)
    owner.register()
    attempt = owner.claim()
    ident = identity(attempt)
    execution = {
        "boot_id": boot_id(),
        "job_name": "Local\\UploadPlatform-" + attempt["execution_id"],
        "result_path": str(tmp_path / "late-result.json"),
        "evidence_path": str(tmp_path / "observation.json"),
        "stderr_path": str(tmp_path / "host.log"),
    }
    assert owner.starting(attempt, execution)
    host = JobHost(execution, lambda data: owner.process_identity(attempt, data))
    try:
        host.send(
            {
                **ident,
                "type": "request",
                "attempt_number": 1,
                "options": {"scenario": "late_result", "duration_seconds": 0.4},
                "result_path": execution["result_path"],
                "evidence_path": execution["evidence_path"],
            }
        )
        hello = HostEvent.model_validate_json(host.messages.get(timeout=5))
        owner.host_event(attempt, hello)
        assert owner.permit(attempt)
        host.send({**ident, "type": "go"})
        progress = HostEvent.model_validate_json(host.messages.get(timeout=5))
        owner.host_event(attempt, progress)
        service.cancel(task_id, uid(), Empty())
        host.send({**ident, "type": "cancel"})
        clock.value += database.config.lease_seconds + 1
        recovery = ExecutionStore(database, clock=clock)
        recovery.register()
        claimed = recovery.recovery_claim()
        assert claimed and service.task(task_id)["status"] == "RECOVERY_PENDING"
        assert recovery.claim() is None
        # The actual old host emits after cancellation and after ownership was revoked.
        while True:
            message = HostEvent.model_validate_json(host.messages.get(timeout=5))
            with pytest.raises(Problem) as rejected:
                owner.host_event(attempt, message)
            assert rejected.value.code == "execution_fenced"
            if message.type == "result":
                break
        wait_until(host.exited)
        assert win32event.WaitForSingleObject(host.process, 0) == win32event.WAIT_OBJECT_0
        result = json.loads(Path(execution["result_path"]).read_text(encoding="utf-8"))
        assert result["outcome"] == "succeeded"
        with pytest.raises(Problem):
            owner.finish(attempt, "succeeded", proof="stale", result=result)
        with pytest.raises(Problem):
            owner.confirm_exit(attempt, "job_empty_and_host_exited")
        if invalid_generation:
            write_json(tmp_path / "original-result.json", result)
            wrong = {
                **result,
                "generation": result["generation"] + 1,
                "history": [
                    {**event, "generation": result["generation"] + 1} for event in result["history"]
                ],
            }
            write_json(execution["result_path"], wrong)
        Supervisor(recovery).recover(claimed)
        task = service.task(task_id)
        assert task["status"] == ("UNCERTAIN" if invalid_generation else "SUCCEEDED")
        if invalid_generation:
            assert task["attempt"]["error"] == "invalid_result"
        assert task["attempt"]["exit_confirmed"]
        assert recovery.recovery_claim() is None
        with pytest.raises(Problem):
            recovery.finish(claimed, "succeeded", proof="duplicate", result=result, recovery=True)
        if not invalid_generation:
            service.submit(manifest["work"]["id"], uid(), Submission(), previous=task_id)
            current = recovery.claim()
            with pytest.raises(Problem):
                recovery.finish(current, "succeeded", proof="old-result", result=result)
            assert service.task(current["task_id"])["status"] == "CLAIMED"
        with database.read() as conn:
            assert len(db.many(conn, select(db.receipts))) == (0 if invalid_generation else 1)
        assert service.task(task_id)["status"] == task["status"]
    finally:
        host.stop(3)
        host.close()
