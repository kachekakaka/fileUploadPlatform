from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select, insert, func
from sqlalchemy.exc import OperationalError, IntegrityError

from conftest import save_manifest
from upload_platform.application.contracts import (
    Registration,
    Submission,
    Empty,
    ScheduleRequest,
    SchedulePatch,
)
from upload_platform.domain.core import Problem, uid, utc
from upload_platform.execution.leases import ExecutionStore
from upload_platform.infrastructure import database as db
from upload_platform.scheduling.runtime import Scheduler, ScheduleService


def workers(database, clock, count=2):
    result = [ExecutionStore(database, clock=clock) for _ in range(count)]
    for worker in result:
        worker.register()
    return result


def begin_running(store, original):
    from upload_platform.execution.protocol import HostEvent, identity

    assert store.starting(original, {})
    store.host_event(original, HostEvent(**identity(original), type="hello", host_sequence=1))
    assert store.permit(original)


def test_claim_race_occupied_expired_slot_and_recovery_fence(database, clock, submit_work, service):
    submit_work()
    submit_work()
    first, second = workers(database, clock)
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda store: store.claim(), [first, second]))
    attempt = next(x for x in results if x)
    owner = first if attempt["worker_id"] == first.worker_id else second
    other = second if owner is first else first
    assert sum(x is not None for x in results) == 1
    clock.value += database.config.lease_seconds + 1
    assert other.claim() is None  # expiry never implicitly releases a slot
    recovery = other.recovery_claim()
    assert recovery and service.task(attempt["task_id"])["status"] == "RECOVERY_PENDING"
    with pytest.raises(Problem, match="所有权"):
        owner.finish(attempt, "retryable", proof="no_execution_intent")
    other.finish(recovery, "retryable", proof="no_execution_intent", recovery=True)
    assert other.claim() is not None


def test_account_fairness_and_partial_unique_constraints(database, clock, service, work_factory):
    with database.transaction() as conn:
        account = uid()
        conn.execute(
            insert(db.accounts).values(
                id=account,
                reference="second",
                platform="fake",
                display_name="second",
                status="ACTIVE",
                concurrency_limit=1,
            )
        )
    for i in range(3):
        directory, manifest = work_factory()
        if i == 2:
            manifest["upload_units"][0]["targets"][0]["account"] = "second"
        save_manifest(directory, manifest)
        service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
        service.submit(manifest["work"]["id"], uid(), Submission())
        clock.value += 1
    worker = workers(database, clock, 1)[0]
    a, b = worker.claim(), worker.claim()
    assert a is not None and b is not None and worker.claim() is None
    assert service.task(b["task_id"])["account_id"] == account
    with database.read() as conn:
        lease = db.one(conn, select(db.leases).where(db.leases.c.attempt_id == a["id"]))
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(insert(db.leases).values(**{**lease, "id": uid()}))
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(
            insert(db.attempts).values(
                **{**a, "id": uid(), "attempt_number": 2, "execution_id": uid()}
            )
        )


@pytest.mark.parametrize(
    "late,expected", [(86399, "CLAIMED"), (86400, "CLAIMED"), (86401, "EXPIRED")]
)
def test_scheduled_lateness_boundary(database, clock, service, work_factory, late, expected):
    directory, manifest = work_factory()
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    task_id = service.submit(
        manifest["work"]["id"],
        uid(),
        Submission(execution_type="scheduled", scheduled_at=utc(clock.value)),
    )[1]["tasks"][0]["task_id"]
    clock.value += late
    worker = workers(database, clock, 1)[0]
    result = worker.claim()
    assert service.task(task_id)["status"] == expected
    assert (result is None) == (expected == "EXPIRED")
    if expected == "EXPIRED":
        with database.read() as conn:
            assert not db.many(conn, select(db.attempts))
            assert not db.many(conn, select(db.leases))


def test_retry_budget_wait_survives_reopen_cancel_and_exhaustion(
    database, clock, service, submit_work
):
    _, _, items = submit_work()
    task_id = items[0]["task_id"]
    worker = workers(database, clock, 1)[0]
    for index, delay in enumerate([30, 120, None]):
        attempt = worker.claim()
        assert attempt["attempt_number"] == index + 1
        worker.finish(attempt, "retryable", proof="no_execution_intent")
        if delay:
            database.initialize()
            assert service.task(task_id)["status"] == "RETRY_WAIT"
            clock.value += delay - 1
            assert worker.claim() is None
            clock.value += 1
        else:
            assert service.task(task_id)["status"] == "FAILED"
            assert worker.claim() is None
    _, _, second = submit_work()
    attempt = worker.claim()
    worker.finish(attempt, "retryable", proof="no_execution_intent")
    service.cancel(second[0]["task_id"], uid(), Empty())
    clock.value += 1000
    assert worker.claim() is None


def test_cancel_claim_race_and_success_wins(database, clock, service, submit_work):
    _, _, items = submit_work()
    task_id = items[0]["task_id"]
    worker = workers(database, clock, 1)[0]
    with ThreadPoolExecutor(2) as pool:
        claim = pool.submit(worker.claim)
        cancel = pool.submit(service.cancel, task_id, uid(), Empty())
        attempt, _ = claim.result(), cancel.result()
    if attempt:
        assert service.task(task_id)["status"] == "CLAIMED"
        worker.finish(attempt, "cancelled", proof="no_execution_intent")
    assert service.task(task_id)["status"] == "CANCELLED"
    _, _, items = submit_work()
    attempt = worker.claim()
    begin_running(worker, attempt)
    service.cancel(items[0]["task_id"], uid(), Empty())
    from upload_platform.execution.protocol import identity

    ident = identity(attempt)
    worker.finish(
        attempt,
        "succeeded",
        proof="test_confirmed_exit",
        result={
            **ident,
            "type": "result",
            "host_sequence": 3,
            "outcome": "succeeded",
            "safe": True,
            "fake": True,
            "receipt": {
                "fake": True,
                "execution_id": attempt["execution_id"],
                "remote_id": "fake-" + attempt["id"],
            },
            "history": [
                {**ident, "type": "hello", "host_sequence": 1},
                {**ident, "type": "progress", "host_sequence": 2},
            ],
        },
    )
    assert service.task(items[0]["task_id"])["status"] == "SUCCEEDED"
    assert service.cancel(items[0]["task_id"], uid(), Empty())[1]["status"] == "SUCCEEDED"
    assert "task.cancel_not_effective" in [
        x["type"] for x in service.event_page(items[0]["task_id"])["items"]
    ]


def make_schedule(service, parent, **kw):
    return ScheduleService(service).create(
        uid(),
        ScheduleRequest(
            parent_directory=str(parent), kind="daily", local_time="00:00:01", timezone="UTC", **kw
        ),
    )[1]


def test_dispatch_multiple_accounts_isolation_new_only_and_restart(
    database, clock, service, work_factory, tmp_path
):
    parent = tmp_path / "parent"
    with database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(),
                reference="second",
                platform="fake",
                display_name="second",
                status="ACTIVE",
                concurrency_limit=1,
            )
        )
    pairs = [work_factory(directory=parent / name) for name in ("a", "b", "c", "broken", "unready")]
    pairs[0][1]["upload_units"][0]["targets"].append({"platform": "fake", "account": "second"})
    save_manifest(*pairs[0])
    (pairs[3][0] / "first.mp4").unlink()  # this test owns the synthetic fixture
    pairs[4][1]["work"]["ready"] = False
    save_manifest(*pairs[4])
    schedule = make_schedule(service, parent)
    scheduler = Scheduler(service, "dispatcher")
    clock.value += 2
    scheduler.tick()
    api = ScheduleService(service)
    with database.read() as conn:
        run = db.one(conn, select(db.runs))
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 4
    detail = api.run_detail(run["id"])
    assert detail["status"] == "COMPLETED_WITH_ERRORS"
    assert len(detail["tasks"]) == 4
    assert [w["status"] for w in detail["works"]].count("ERROR") == 1
    assert [w["status"] for w in detail["works"]].count("SKIPPED_NOT_READY") == 1
    (pairs[3][0] / "first.mp4").write_bytes(b"fixed-synthetic")
    pairs[4][1]["work"]["ready"] = True
    save_manifest(*pairs[4])
    database.initialize()
    clock.value += 86400
    scheduler.tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 6
    assert api.detail(schedule["id"])["next_fire_at"] > clock.value


def test_concurrent_manual_daily_weekly_shared_identity(
    database, clock, service, work_factory, tmp_path
):
    parent = tmp_path / "parent"
    directory, manifest = work_factory(directory=parent / "work", independent=True)
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    make_schedule(service, parent)
    ScheduleService(service).create(
        uid(),
        ScheduleRequest(
            parent_directory=str(parent),
            kind="weekly",
            weekday=1,
            local_time="00:00:01",
            timezone="UTC",
        ),
    )
    clock.value += 2
    schedulers = [Scheduler(service, "a"), Scheduler(service, "b")]
    with ThreadPoolExecutor(3) as pool:
        jobs = [pool.submit(s.tick) for s in schedulers]
        jobs.append(pool.submit(service.submit, manifest["work"]["id"], uid(), Submission()))
        [f.result() for f in jobs]
    for s in schedulers:
        s.tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 2
        assert conn.execute(select(func.count()).select_from(db.runs)).scalar_one() == 2
        assert conn.execute(select(func.count()).select_from(db.run_tasks)).scalar_one() == 4


def test_persistent_candidate_list_takeover_and_old_owner_fenced(
    database, clock, service, work_factory, tmp_path
):
    parent = tmp_path / "parent"
    work_factory(directory=parent / "a")
    make_schedule(service, parent)
    clock.value += 2
    old = Scheduler(service, "old", lease_seconds=10)
    old.evaluate()
    run = old.claim_run()

    def crash(point):
        if point == "candidates_saved":
            raise RuntimeError("process interrupted")

    service.fault = crash
    with pytest.raises(RuntimeError):
        old.dispatch(run)
    service.fault = lambda _: None
    work_factory(directory=parent / "b")
    clock.value += 11
    new = Scheduler(service, "new", lease_seconds=10)
    reclaimed = new.claim_run()
    with pytest.raises(Problem, match="所有权"):
        old.record_error(run, "stale", "ERROR", {})
    new.dispatch(reclaimed)
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 1
    clock.value += 86400
    new.tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 2


@pytest.mark.parametrize(
    "late,status", [(86399, "PENDING"), (86400, "PENDING"), (86401, "SKIPPED")]
)
def test_weekly_lateness_boundaries(database, clock, service, tmp_path, late, status):
    schedule = ScheduleService(service).create(
        uid(),
        ScheduleRequest(
            parent_directory=str(tmp_path),
            kind="weekly",
            weekday=1,
            local_time="00:00:01",
            timezone="UTC",
        ),
    )[1]
    clock.value = schedule["next_fire_at"] + late
    Scheduler(service, "owner").evaluate()
    with database.read() as conn:
        assert db.one(conn, select(db.runs))["status"] == status
    database.initialize()
    Scheduler(service, "owner").evaluate()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.runs)).scalar_one() == 1


def test_daily_missed_points_merge_latest_and_rule_patch(database, clock, service, tmp_path):
    api = ScheduleService(service)
    schedule = make_schedule(service, tmp_path)
    clock.value = schedule["next_fire_at"] + 3 * 86400 + 50
    Scheduler(service, "owner").evaluate()
    with database.read() as conn:
        run = db.one(conn, select(db.runs))
        assert run["merged_count"] == 4
        assert run["planned_at"] == schedule["next_fire_at"] + 3 * 86400
    disabled = api.patch(schedule["id"], uid(), SchedulePatch(expected_version=1, enabled=False))[1]
    with pytest.raises(Problem):
        api.patch(schedule["id"], uid(), SchedulePatch(expected_version=1, enabled=True))
    clock.value += 5 * 86400
    enabled = api.patch(
        schedule["id"], uid(), SchedulePatch(expected_version=disabled["version"], enabled=True)
    )[1]
    assert enabled["next_fire_at"] > clock.value
    Scheduler(service, "owner").evaluate()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.runs)).scalar_one() == 1


def test_direct_children_duplicate_id_and_no_background_prepare(
    database, clock, service, work_factory, tmp_path
):
    parent = tmp_path / "parent"
    _, first = work_factory(directory=parent / "a")
    work_factory(directory=parent / "b", work_id=first["work"]["id"])
    nested, _ = work_factory(directory=parent / "container" / "deep")
    missing, raw = work_factory(directory=parent / "no-id")
    del raw["work"]["id"]
    save_manifest(missing, raw)
    (parent / "loose.mp4").write_bytes(b"not-a-work")
    make_schedule(service, parent)
    clock.value += 2
    Scheduler(service, "owner").tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 1
        assert conn.execute(select(func.count()).select_from(db.run_works)).scalar_one() == 4
    assert "id:" not in (missing / "upload.yaml").read_text(encoding="utf-8").split("assets:")[0]
    assert (nested / "first.mp4").exists()


def test_dispatch_transaction_failure_isolated_and_database_failure_stops(
    database, clock, service, work_factory, tmp_path
):
    parent = tmp_path / "parent"
    for name in ("a", "b", "c"):
        work_factory(directory=parent / name)
    make_schedule(service, parent)
    clock.value += 2
    counter = 0

    def fault(point):
        nonlocal counter
        if point == "dispatch_before_commit":
            counter += 1
            if counter == 1:
                raise RuntimeError("isolated rollback")
            if counter == 3:
                raise OperationalError("injected", {}, RuntimeError("database down"))

    service.fault = fault
    scheduler = Scheduler(service, "owner", lease_seconds=5)
    with pytest.raises(OperationalError):
        scheduler.tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 1
        run = db.one(conn, select(db.runs))
        assert run["status"] == "DISPATCHING"
        assert len(db.many(conn, select(db.run_works))) == 2
    service.fault = lambda _: None
    clock.value += 6
    scheduler.tick()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.tasks)).scalar_one() == 2
