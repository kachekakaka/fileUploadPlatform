from concurrent.futures import ThreadPoolExecutor
import threading
import time

import pytest
from sqlalchemy import event, select

from upload_platform.application.contracts import Registration, Submission
from upload_platform.domain.core import uid, utc
from upload_platform.execution.leases import ExecutionStore
from upload_platform.infrastructure import database as db
from upload_platform.scheduling.runtime import ScheduleService, Scheduler
from upload_platform.application.contracts import ScheduleRequest


def after_write_lock(database, clock, operation, advance):
    """Block a real second SQLite connection at BEGIN IMMEDIATE, then advance UTC."""
    waiting = threading.Event()
    main_thread = threading.get_ident()

    def entering(conn, cursor, statement, parameters, context, executemany):
        if statement == "BEGIN IMMEDIATE" and threading.get_ident() != main_thread:
            waiting.set()

    event.listen(database.engine, "before_cursor_execute", entering)
    try:
        with ThreadPoolExecutor(1) as pool:
            with database.transaction():
                future = pool.submit(operation)
                assert waiting.wait(5)
                time.sleep(0.05)
                assert not future.done()
                clock.value += advance
            return future.result(timeout=10)
    finally:
        event.remove(database.engine, "before_cursor_execute", entering)


def test_claim_rechecks_expiry_after_write_lock(database, service, clock, work_factory):
    directory, manifest = work_factory()
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    task_id = service.submit(
        manifest["work"]["id"],
        uid(),
        Submission(execution_type="scheduled", scheduled_at=utc(clock())),
    )[1]["tasks"][0]["task_id"]
    owner = ExecutionStore(database, clock=clock)
    owner.register()
    clock.value += 86400
    assert after_write_lock(database, clock, owner.claim, 5) is None
    assert service.task(task_id)["status"] == "EXPIRED"
    with database.read() as conn:
        assert db.many(conn, select(db.attempts)) == []
        assert db.many(conn, select(db.leases)) == []
    assert [e["type"] for e in service.event_page(task_id)["items"]].count("task.expired") == 1


def test_lease_and_retry_times_follow_lock_acquisition(database, service, clock, submit_work):
    submit_work()
    owner = ExecutionStore(database, clock=clock)
    owner.register()
    attempt = after_write_lock(database, clock, owner.claim, 5)
    assert attempt["started_at"] == clock()
    after_write_lock(database, clock, lambda: owner.heartbeat(attempt), 0.5)
    with database.read() as conn:
        assert all(
            lease["expires_at"] == clock() + database.config.lease_seconds
            for lease in db.many(conn, select(db.leases))
        )
    after_write_lock(
        database,
        clock,
        lambda: owner.finish(attempt, "retryable", proof="no_execution_intent"),
        0.5,
    )
    assert service.task(attempt["task_id"])["next_attempt_at"] == clock() + 30
    clock.value += 29
    assert owner.claim() is None
    clock.value += 1
    assert owner.claim()["attempt_number"] == 2


def test_recovery_claim_and_renew_use_time_after_lock(database, clock, submit_work):
    submit_work()
    owner = ExecutionStore(database, clock=clock)
    owner.register()
    original = owner.claim()
    recovery = ExecutionStore(database, clock=clock)
    recovery.register()
    attempt = after_write_lock(
        database, clock, recovery.recovery_claim, database.config.lease_seconds + 1
    )
    assert attempt["id"] == original["id"]
    assert attempt["recovery_expires"] == clock() + database.config.lease_seconds
    after_write_lock(database, clock, lambda: recovery.heartbeat(attempt, recovery=True), 0.5)
    with database.read() as conn:
        assert (
            db.get(conn, db.attempts, attempt["id"])["recovery_expires"]
            == clock() + database.config.lease_seconds
        )


@pytest.mark.parametrize("advance,expected,count", [(2, "SKIPPED", 1), (86400, "PENDING", 2)])
def test_schedule_rechecks_window_and_latest_point_after_lock(
    database, service, clock, tmp_path, advance, expected, count
):
    rule = ScheduleService(service).create(
        uid(),
        ScheduleRequest(
            parent_directory=str(tmp_path),
            kind="daily",
            local_time="00:00:01",
            timezone="UTC",
            max_lateness_seconds=10,
        ),
    )[1]
    clock.value = rule["next_fire_at"] + 10
    scheduler = Scheduler(service, uid())
    after_write_lock(database, clock, scheduler.evaluate, advance)
    with database.read() as conn:
        run = db.one(conn, select(db.runs))
        assert run["status"] == expected
        assert run["started_at"] == clock()
        assert run["merged_count"] == count
        assert db.get(conn, db.schedules, rule["id"])["next_fire_at"] > clock()
    if expected == "PENDING":
        claimed = after_write_lock(database, clock, scheduler.claim_run, 5)
        assert claimed["expires_at"] == clock() + scheduler.lease_seconds
