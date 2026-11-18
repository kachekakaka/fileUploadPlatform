import json
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from sqlalchemy import select

from upload_platform.application.accounts import Accounts, MaintenanceRequest
from upload_platform.application.contracts import ResolutionRequest
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.domain.core import Problem, uid
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db
from test_bilibili_runtime import setup_bilibili as setup_bilibili

import pytest


def test_review_failure_preserves_previous_reliable_status_and_query_replay(setup_bilibili):
    database, service, store, task_id, control = setup_bilibili
    Supervisor(store).execute(store.claim())
    queries = RemoteQueries(service)
    control.write_text(json.dumps({"review_state": -4}))
    key = uid()
    first = queries.query(task_id, key)[1]
    assert first["result"]["review_status"] == -4
    assert queries.query(task_id, key)[1] == first
    control.write_text(json.dumps({"query_failure": True}))
    failed = queries.query(task_id, uid())[1]
    assert failed["error"] == "query_inconclusive"
    with database.read() as conn:
        receipt = db.one(conn, select(db.receipts).where(db.receipts.c.task_id == task_id))
        assert receipt["review_status"] == -4
        assert (
            receipt["last_query_at"] is not None and receipt["query_error"] == "query_inconclusive"
        )
        assert len(db.many(conn, select(db.remote_queries))) == 2
    assert service.task(task_id)["status"] == "SUCCEEDED"


def test_query_is_excluded_by_account_maintenance(setup_bilibili):
    _, service, store, task_id, _ = setup_bilibili
    Supervisor(store).execute(store.claim())
    Accounts(service).create(
        uid(), MaintenanceRequest(account_id=service.task(task_id)["account_id"])
    )
    with pytest.raises(Problem) as error:
        RemoteQueries(service).query(task_id, uid())
    assert error.value.code == "maintenance_active"


def test_manual_bilibili_failure_preserves_uncertain_attempt(setup_bilibili):
    database, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({"crash": True}))
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    task = service.task(task_id)
    result = service.resolve(
        task_id,
        uid(),
        ResolutionRequest(
            conclusion="failed",
            reason="已人工核验创作中心",
            evidence_refs=["manual-review:2026-11-17"],
            expected_task_version=task["version"],
        ),
    )[1]
    assert result["status"] == "FAILED"
    with database.read() as conn:
        assert db.get(conn, db.attempts, attempt["id"])["status"] == "UNCERTAIN"
        resolution = db.one(conn, select(db.resolutions).where(db.resolutions.c.task_id == task_id))
        assert resolution["source"] == "manual-bilibili"
    assert store.claim() is None


def test_invalid_credential_schedules_one_noninteractive_renewal(setup_bilibili):
    database, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({"outcome": "retryable", "error_code": "CREDENTIAL_INVALID"}))
    Supervisor(store).execute(store.claim())
    with database.read() as conn:
        operations = db.many(conn, select(db.account_maintenance))
        assert len(operations) == 1 and operations[0]["kind"] == "renew"
    assert store.claim() is None
    control.write_text("{}")
    Accounts(service).run_automatic_once()
    with database.read() as conn:
        assert db.get(conn, db.account_maintenance, operations[0]["id"])["status"] == "SUCCEEDED"
        assert db.many(conn, select(db.account_blocks)) == []


def test_late_query_cannot_replace_newer_review_generation(setup_bilibili, tmp_path):
    database, service, store, task_id, control = setup_bilibili
    Supervisor(store).execute(store.claim())
    gate = tmp_path / "old-query-release"
    control.write_text(json.dumps({"query_gate": str(gate), "review_state": -4}))
    with ThreadPoolExecutor() as pool:
        old = pool.submit(RemoteQueries(service).query, task_id, uid())
        try:
            deadline = time.monotonic() + 10
            ready = False
            while time.monotonic() < deadline:
                with database.read() as conn:
                    operation = db.one(conn, select(db.remote_queries))
                if (
                    operation
                    and Path(operation["execution"]["result_path"])
                    .with_name("query-started")
                    .exists()
                ):
                    ready = True
                    break
                time.sleep(0.01)
            assert ready
            control.write_text(json.dumps({"review_state": 0}))
            newer = RemoteQueries(service).query(task_id, uid())[1]
            assert newer["generation"] == 2
        finally:
            gate.touch()
        assert old.result(timeout=15)[1]["result"]["review_status"] == -4
    with database.read() as conn:
        receipt = db.one(conn, select(db.receipts).where(db.receipts.c.task_id == task_id))
        assert receipt["query_generation"] == 2 and receipt["review_status"] == 0
