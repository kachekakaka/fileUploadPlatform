from concurrent.futures import ThreadPoolExecutor
import time

import pytest
from sqlalchemy import select

from upload_platform.application.contracts import Empty, ResolutionRequest, Submission
from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db


def wait_until(fn, timeout=12):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = fn()
        if value:
            return value
        time.sleep(0.03)
    raise AssertionError("condition timed out")


@pytest.mark.parametrize(
    "scenario,expected",
    [
        ("success", "SUCCEEDED"),
        ("retryable", "RETRY_WAIT"),
        ("permanent", "FAILED"),
        ("crash", "UNCERTAIN"),
        ("invalid_result", "UNCERTAIN"),
        ("crash_before_go", "RETRY_WAIT"),
    ],
)
def test_real_host_result_classification(database, submit_work, scenario, expected):
    _, _, tasks = submit_work(scenario=scenario)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    assert attempt
    Supervisor(store).execute(attempt)
    task = Service(database).task(tasks[0]["task_id"])
    assert task["status"] == expected, task
    assert task["attempt"]["exit_confirmed"] is True
    with database.read() as conn:
        assert all(row["released_at"] is not None for row in db.many(conn, select(db.leases)))
    if expected == "SUCCEEDED":
        assert task["receipt"]["fake"] is True
        assert task["receipt"]["source"] == "adapter"
    else:
        assert task["receipt"] is None


@pytest.mark.parametrize(
    "scenario,expected",
    [("cancel_safe", "CANCELLED"), ("cancel_uncertain", "UNCERTAIN"), ("child", "CANCELLED")],
)
def test_cancel_requires_real_tree_exit(database, submit_work, scenario, expected):
    import win32api
    import win32event

    _, _, tasks = submit_work(scenario=scenario, duration=3)
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    handles = []
    try:
        with ThreadPoolExecutor(1) as pool:
            future = pool.submit(Supervisor(store).execute, attempt)
            wait_until(lambda: service.task(tasks[0]["task_id"])["status"] == "RUNNING")
            execution = service.task(tasks[0]["task_id"])["attempt"]["execution"]
            handles.append(win32api.OpenProcess(0x00100000, False, execution["pid"]))
            if scenario == "child":
                child_pid = wait_until(
                    lambda: next(
                        (
                            e["payload"]["child_pid"]
                            for e in service.event_page(tasks[0]["task_id"])["items"]
                            if e["payload"].get("child_pid")
                        ),
                        None,
                    )
                )
                handles.append(win32api.OpenProcess(0x00100000, False, child_pid))
            response = service.cancel(tasks[0]["task_id"], uid(), Empty())[1]
            assert response["status"] in ("RUNNING", "UNCERTAIN", "SUCCEEDED")
            future.result(timeout=15)
        task = service.task(tasks[0]["task_id"])
        assert task["status"] == expected, task
        assert all(
            win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
            for handle in handles
        )
    finally:
        for handle in handles:
            handle.Close()


def test_timeout_before_permission_safe_after_permission_uncertain(database, work_factory, service):
    from dataclasses import replace
    from upload_platform.application.contracts import Registration

    for scenario, expected in [("no_handshake", "RETRY_WAIT"), ("hang", "UNCERTAIN")]:
        directory, manifest = work_factory(scenario=scenario)
        service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
        task_id = service.submit(
            manifest["work"]["id"],
            uid(),
            Submission(
                retry_policy={"max_attempts": 3, "retry_delays_seconds": [999999999, 999999999]}
            ),
        )[1]["tasks"][0]["task_id"]
        store = ExecutionStore(database)
        store.register()
        supervisor = Supervisor(store)
        supervisor.config = replace(database.config, handshake_timeout=1, execution_timeout=1.5)
        supervisor.execute(store.claim())
        assert Service(database).task(task_id)["status"] == expected


def test_uncertain_manual_evidence_preserves_attempt_and_explicit_reupload(database, submit_work):
    _, manifest, tasks = submit_work(scenario="crash")
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    Supervisor(store).execute(store.claim())
    task_id = tasks[0]["task_id"]
    task = service.task(task_id)
    assert task["status"] == "UNCERTAIN"
    with pytest.raises(Problem):
        service.submit(manifest["work"]["id"], uid(), Submission(), previous=task_id)
    uncertain = ResolutionRequest(
        conclusion="uncertain", reason="继续核对", expected_task_version=task["version"]
    )
    service.resolve(task_id, uid(), uncertain)
    task = service.task(task_id)
    with pytest.raises(Problem, match="依据"):
        service.resolve(
            task_id,
            uid(),
            ResolutionRequest(
                conclusion="failed",
                reason="无依据",
                evidence_refs=[],
                expected_task_version=task["version"],
            ),
        )
    key = uid()
    request = ResolutionRequest(
        conclusion="succeeded",
        reason="读取 Fake 观察记录",
        evidence_refs=[task["evidence_ref"]],
        expected_task_version=task["version"],
    )
    result = service.resolve(task_id, key, request)
    assert service.resolve(task_id, key, request) == result
    assert service.task(task_id)["status"] == "SUCCEEDED"
    assert service.task(task_id)["attempt"]["status"] == "UNCERTAIN"
    assert service.task(task_id)["receipt"]["source"] == "manual"
    assert service.task(task_id)["receipt"]["fake"] is True
    service.submit(manifest["work"]["id"], uid(), Submission(), previous=task_id)


def test_result_file_recovered_once_and_stale_worker_cannot_commit(database, submit_work):
    _, _, tasks = submit_work()
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()

    def crash(point):
        if point == "after_result_before_commit":
            raise RuntimeError("injected supervisor crash before commit")

    Supervisor(store, crash).execute(attempt)
    assert service.task(tasks[0]["task_id"])["status"] == "RUNNING"
    time.sleep(database.config.lease_seconds + 0.1)
    recovery_store = ExecutionStore(database)
    recovery_store.register()
    recovery = recovery_store.recovery_claim()
    Supervisor(recovery_store).recover(recovery)
    assert service.task(tasks[0]["task_id"])["status"] == "SUCCEEDED"
    with pytest.raises(Problem):
        store.finish(attempt, "uncertain", proof="stale")
    assert recovery_store.recovery_claim() is None
    with database.read() as conn:
        assert len(db.many(conn, select(db.receipts))) == 1


def test_failed_manual_resolution_race_rollback_and_no_automatic_retry(database, submit_work):
    _, manifest, tasks = submit_work(scenario="cancel_uncertain", duration=3)
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    task_id = tasks[0]["task_id"]
    with ThreadPoolExecutor(1) as pool:
        future = pool.submit(Supervisor(store).execute, store.claim())
        wait_until(lambda: service.task(task_id)["status"] == "RUNNING")
        service.cancel(task_id, uid(), Empty())
        future.result(timeout=15)
    original = service.task(task_id)
    assert original["status"] == "UNCERTAIN"
    request = ResolutionRequest(
        conclusion="failed",
        reason="模拟观察显示未成功",
        evidence_refs=[original["evidence_ref"]],
        expected_task_version=original["version"],
    )

    def fail(point):
        if point == "before_commit":
            raise RuntimeError("resolution transaction interrupted")

    with pytest.raises(RuntimeError):
        Service(database, fault=fail).resolve(task_id, uid(), request)
    assert service.task(task_id)["version"] == original["version"]
    assert service.listing(db.resolutions)["items"] == []

    def resolve(_):
        try:
            return service.resolve(task_id, uid(), request)
        except Problem as exc:
            return exc.status

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(resolve, range(2)))
    assert sum(result == 409 for result in results) == 1
    assert service.task(task_id)["status"] == "FAILED"
    assert service.task(task_id)["attempt"]["status"] == "UNCERTAIN"
    assert store.claim() is None
    assert (
        service.submit(manifest["work"]["id"], uid(), Submission(), previous=task_id)[1]["tasks"][
            0
        ]["submission_round"]
        == 2
    )


def test_unconfirmed_exit_refuses_resolution_and_invalid_late_message(database, submit_work):
    from sqlalchemy import update
    from upload_platform.execution.protocol import HostEvent, identity

    _, _, tasks = submit_work(scenario="crash")
    service = Service(database)
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    task = service.task(tasks[0]["task_id"])
    assert task["status"] == "UNCERTAIN"
    with pytest.raises(Problem):
        store.host_event(
            attempt, HostEvent(**identity(attempt), type="progress", host_sequence=99, progress=100)
        )
    # A corrupt/missing proof must not be repaired by an operator's conclusion.
    with database.transaction() as conn:
        conn.execute(
            update(db.attempts)
            .where(db.attempts.c.id == attempt["id"])
            .values(exit_confirmed=False)
        )
    with pytest.raises(Problem):
        service.resolve(
            task["id"],
            uid(),
            ResolutionRequest(
                conclusion="succeeded",
                reason="不充分依据",
                evidence_refs=[task["evidence_ref"]],
                expected_task_version=task["version"],
            ),
        )
    assert service.task(task["id"])["status"] == "UNCERTAIN"
