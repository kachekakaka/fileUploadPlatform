from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import sqlite3

import pytest
from sqlalchemy import select, insert, update, func
from sqlalchemy.exc import IntegrityError

from conftest import save_manifest
from upload_platform.application.contracts import Registration, Submission, Empty
from upload_platform.application.service import Service
from upload_platform.config import Config
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db


def count(database, table):
    with database.read() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def test_initialize_concurrent_reopen_and_connection_pragmas(config):
    def initialize(_):
        database = db.Database(config)
        try:
            database.initialize()
        finally:
            database.close()

    with ThreadPoolExecutor(4) as pool:
        list(pool.map(initialize, range(4)))
    database = db.Database(config)
    database.initialize()
    assert count(database, db.accounts) == 1
    with database.read() as conn:
        for name, expected in [
            ("foreign_keys", 1),
            ("synchronous", 2),
            ("busy_timeout", 5000),
            ("journal_mode", "wal"),
        ]:
            assert conn.exec_driver_sql("PRAGMA " + name).scalar_one() == expected
    database.close()


@pytest.mark.parametrize("kind", ["foreign", "version", "index"])
def test_unknown_database_refused_without_changes(tmp_path, kind):
    config = Config(tmp_path / "existing")
    config.data_dir.mkdir()
    database = db.Database(config)
    if kind != "foreign":
        database.initialize()
    database.close()
    with sqlite3.connect(config.database) as conn:
        if kind == "foreign":
            conn.execute("CREATE TABLE users (id INTEGER)")
            conn.execute("INSERT INTO users VALUES (42)")
        elif kind == "version":
            conn.execute("UPDATE schema_version SET version=99")
        else:
            conn.execute("DROP INDEX one_occupied_slot")
    before = config.database.read_bytes()
    database = db.Database(config)
    with pytest.raises(Problem):
        database.initialize()
    database.close()
    assert config.database.read_bytes() == before


def test_replay_does_not_read_moved_files_and_conflict(service, work_factory):
    directory, manifest = work_factory()
    request = Registration(directory=str(directory), manifest=manifest)
    key = uid()
    first = service.registration(key, request)
    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    assert service.registration(key, request) == first
    changed = deepcopy(manifest)
    changed["work"]["title"] = "不同请求"
    with pytest.raises(Problem, match="Key") as conflict:
        service.registration(key, Registration(directory=str(directory), manifest=changed))
    assert conflict.value.status == 409


def test_submit_concurrent_shared_ledger_and_request_key(service, submit_work, database):
    _, manifest, first = submit_work(independent=True)
    key, work_id = uid(), manifest["work"]["id"]
    with ThreadPoolExecutor(8) as pool:
        results = list(
            pool.map(
                lambda i: service.submit(work_id, key if i < 4 else uid(), Submission()), range(8)
            )
        )
    assert all(
        [t["task_id"] for t in result[1]["tasks"]] == [t["task_id"] for t in first]
        for result in results
    )
    assert count(database, db.tasks) == 2
    assert count(database, db.ledgers) == 2
    assert results[0] == results[1]


def test_snapshot_relocation_and_new_target(service, submit_work, database):
    directory, manifest, first = submit_work()
    old_id = first[0]["task_id"]
    old_snapshot = service.task(old_id)["snapshot"]
    with database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(),
                platform="fake",
                reference="second",
                display_name="second",
                status="ACTIVE",
                concurrency_limit=1,
            )
        )
    moved = directory.with_name(directory.name + "-moved")
    directory.rename(moved)
    manifest["work"]["title"] = "新的标题"
    manifest["upload_units"][0]["targets"].append({"platform": "fake", "account": "second"})
    save_manifest(moved, manifest)
    service.registration(uid(), Registration(directory=str(moved), manifest=manifest))
    result = service.submit(manifest["work"]["id"], uid(), Submission())[1]["tasks"]
    assert result[0]["created"] is False
    assert result[0]["snapshot"] == old_snapshot
    assert result[1]["snapshot"]["directory"] == str(moved.resolve())
    assert result[1]["snapshot"]["metadata"]["title"] == "新的标题"
    assert service.task(old_id)["snapshot"] == old_snapshot
    assert len(list(moved.glob("*.mp4"))) == 2
    assert not list(database.config.data_dir.rglob("*.mp4"))


@pytest.mark.parametrize("point", ["after_task", "before_idempotency", "before_commit"])
def test_submission_failure_has_no_half_batch(service, work_factory, database, point):
    directory, manifest = work_factory(independent=True)
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    before = {
        table.name: count(database, table)
        for table in [db.tasks, db.ledgers, db.events, db.outbox, db.idempotency, db.assets]
    }

    def fail(name):
        if name == point:
            raise RuntimeError("injected")

    failing = Service(database, service.clock, fail)
    with pytest.raises(RuntimeError):
        failing.submit(manifest["work"]["id"], uid(), Submission())
    for table in [db.tasks, db.ledgers, db.events, db.outbox, db.idempotency, db.assets]:
        assert count(database, table) == before[table.name]


def test_response_lost_after_commit_restores_original(service, work_factory, database):
    directory, manifest = work_factory()
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))

    def fail(name):
        if name == "after_commit":
            raise ConnectionError("response lost")

    key = uid()
    with pytest.raises(ConnectionError):
        Service(database, service.clock, fail).submit(manifest["work"]["id"], key, Submission())
    (directory / "upload.yaml").rename(directory / "removed.yaml")
    result = service.submit(manifest["work"]["id"], key, Submission())
    assert len(result[1]["tasks"]) == 1
    assert count(database, db.tasks) == 1


def test_resubmit_concurrent_switch_once_and_history(service, submit_work, database):
    _, manifest, items = submit_work()
    old = items[0]["task_id"]
    service.cancel(old, uid(), Empty())

    def attempt(_):
        try:
            return service.submit(manifest["work"]["id"], uid(), Submission(), previous=old)
        except Problem as exc:
            return exc.status

    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(attempt, range(2)))
    assert sum(result == 409 for result in results) == 1
    result = next(result for result in results if result != 409)
    new = result[1]["tasks"][0]
    assert new["submission_round"] == 2 and new["previous_task_id"] == old
    assert service.task(old)["status"] == "CANCELLED"
    assert (
        service.submit(manifest["work"]["id"], uid(), Submission())[1]["tasks"][0]["task_id"]
        == new["task_id"]
    )
    assert count(database, db.tasks) == 2


def test_constraints_rollback_and_persist(service, submit_work, database, config):
    _, _, items = submit_work()
    task = service.task(items[0]["task_id"])
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == task["id"]).values(status="UNKNOWN"))
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == task["id"]).values(account_id=uid()))
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(update(db.tasks).where(db.tasks.c.id == task["id"]).values(attempt_count=-1))
    reopened = db.Database(config)
    reopened.initialize()
    assert Service(reopened).task(task["id"])["status"] == "QUEUED"
    reopened.close()


def test_no_targets_registered_but_not_submitted(service, work_factory, database):
    directory, manifest = work_factory()
    manifest["upload_units"][0]["targets"] = []
    save_manifest(directory, manifest)
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    with pytest.raises(Problem, match="目标"):
        service.submit(manifest["work"]["id"], uid(), Submission())
    assert count(database, db.tasks) == 0


def test_account_alias_duplicates(service, work_factory, database):
    directory, manifest = work_factory()
    with database.read() as conn:
        account = db.one(conn, select(db.accounts))
    manifest["upload_units"][0]["targets"].append({"platform": "fake", "account": account["id"]})
    with pytest.raises(Problem, match="重复"):
        service.registration(uid(), Registration(directory=str(directory), manifest=manifest))


def test_wrong_work_and_removed_resubmit_rejected(service, submit_work):
    directory, manifest, items = submit_work()
    changed = deepcopy(manifest)
    changed["work"]["id"] = uid()
    with pytest.raises(Problem, match="编号"):
        service.submit(
            manifest["work"]["id"], uid(), Submission(directory=str(directory), manifest=changed)
        )
    old = items[0]["task_id"]
    service.cancel(old, uid(), Empty())
    manifest["upload_units"][0]["mode"] = "independent"
    save_manifest(directory, manifest)
    with pytest.raises(Problem, match="改变"):
        service.submit(manifest["work"]["id"], uid(), Submission(), previous=old)
