import json
import time
import queue

import pytest
from sqlalchemy import select

from upload_platform.application.accounts import (
    Accounts,
    MaintenanceRequest,
    ProcessIdentity,
    AccountEnabled,
)
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import JobHost
from test_bilibili_runtime import setup_bilibili as setup_bilibili


def run_operation(accounts, operation, before_complete=None):
    started = accounts.start(operation["id"], uid())[1]

    def persist(execution):
        accounts.process(
            operation["id"],
            uid(),
            ProcessIdentity(**{k: execution[k] for k in ("pid", "creation_time", "assigned")}),
        )

    host = JobHost(started["execution"], persist, command=started["command"])
    try:
        host.send(started["engine_request"])
        deadline = time.monotonic() + 15
        while (not host.exited() or host.active_count()) and time.monotonic() < deadline:
            try:
                line = host.messages.get(timeout=0.05)
            except queue.Empty:
                continue
            if line and json.loads(line).get("type") == "account_identified":
                identified = accounts.identified(operation["id"], uid())[1]
                assert identified["ready"]
                host.send(
                    {
                        "type": "maintenance_ready",
                        "operation_id": operation["id"],
                        "verified_mid": identified["verified_mid"],
                    }
                )
        assert host.exited() and host.active_count() == 0
        if before_complete:
            before_complete()
        return accounts.complete(operation["id"], uid())[1]
    finally:
        host.close()


def test_registration_same_mid_retains_account_and_manual_disable(setup_bilibili):
    database, service, store, task_id, control = setup_bilibili
    accounts = Accounts(service)
    account_id = service.task(task_id)["account_id"]
    accounts.enabled(account_id, uid(), AccountEnabled(enabled=False))
    operation = accounts.create(uid(), MaintenanceRequest())[1]
    result = run_operation(accounts, operation)
    assert result["status"] == "SUCCEEDED"
    assert result["account_id"] == account_id
    with database.read() as conn:
        values = db.many(conn, select(db.accounts).where(db.accounts.c.platform == "bilibili"))
        assert len(values) == 1 and not values[0]["enabled"]
        assert values[0]["credential_reference"] == result["credential_reference"]
    assert store.claim() is None


def test_maintenance_blocks_new_claim_and_waits_existing_lease(setup_bilibili):
    database, service, store, task_id, _ = setup_bilibili
    attempt = store.claim()
    accounts = Accounts(service)
    operation = accounts.create(
        uid(), MaintenanceRequest(account_id=service.task(task_id)["account_id"])
    )[1]
    assert operation["execution"]["waiting_attempts"] == [attempt["id"]]
    assert store.claim() is None
    with pytest.raises(Problem, match="正在等待"):
        accounts.start(operation["id"], uid())
    # No engine was launched: a valid pre-execution failure can release this attempt.
    store.finish(attempt, "permanent", proof="no_execution_intent")
    assert run_operation(accounts, operation)["status"] == "SUCCEEDED"


@pytest.mark.parametrize("invalid", [False, True])
def test_failed_login_checks_old_credential_and_retains_failure(setup_bilibili, invalid):
    database, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({"maintenance_failure": True, "invalid_credential": invalid}))
    accounts = Accounts(service)
    account_id = service.task(task_id)["account_id"]
    operation = accounts.create(uid(), MaintenanceRequest(account_id=account_id))[1]
    result = run_operation(accounts, operation)
    assert result["status"] == "FAILED" and result["result"]["outcome"] == "failed"
    with database.read() as conn:
        account = db.get(conn, db.accounts, account_id)
        blocks = db.many(
            conn, select(db.account_blocks).where(db.account_blocks.c.account_id == account_id)
        )
    assert account["credential_reference"] == operation["old_credential_reference"]
    assert bool(blocks) == invalid
    if invalid:
        assert store.claim() is None
        # A failed verification must not permanently prevent a new explicit login.
        control.write_text("{}")
        next_operation = accounts.create(uid(), MaintenanceRequest(account_id=account_id))[1]
        assert run_operation(accounts, next_operation)["status"] == "SUCCEEDED"
    assert store.claim() is not None


def test_process_identity_cannot_be_replaced_and_start_is_idempotent(setup_bilibili):
    _, service, _, task_id, _ = setup_bilibili
    accounts = Accounts(service)
    key = uid()
    body = MaintenanceRequest(account_id=service.task(task_id)["account_id"])
    operation = accounts.create(key, body)[1]
    assert accounts.create(key, body)[1] == operation
    start_key = uid()
    started = accounts.start(operation["id"], start_key)[1]
    assert accounts.start(operation["id"], start_key)[1] == started
    with pytest.raises(Problem):
        accounts.start(operation["id"], uid())
    with pytest.raises(Problem) as error:
        accounts.complete(operation["id"], uid())
    assert error.value.code == "maintenance_exit_unconfirmed"


@pytest.mark.parametrize("observed", [False, True])
def test_credential_publication_survives_core_commit_failure_without_relogin(
    setup_bilibili, observed
):
    database, service, store, task_id, _ = setup_bilibili
    accounts = Accounts(service)
    account_id = service.task(task_id)["account_id"]
    operation = accounts.create(uid(), MaintenanceRequest(account_id=account_id))[1]

    def inject():
        if observed:
            deadline = time.monotonic() + 5
            while (
                not accounts.get(operation["id"])["execution"].get("exit_confirmed")
                and time.monotonic() < deadline
            ):
                time.sleep(0.01)
            assert accounts.get(operation["id"])["execution"]["exit_confirmed"]

        def fail(point):
            if point == "before_idempotency":
                raise RuntimeError("synthetic commit failure")

        service.fault = fail

    with pytest.raises(RuntimeError, match="synthetic"):
        run_operation(accounts, operation, inject)
    service.fault = lambda point: None
    with database.read() as conn:
        account = db.get(conn, db.accounts, account_id)
        saved = db.get(conn, db.account_maintenance, operation["id"])
        assert account["credential_reference"] == operation["old_credential_reference"]
        assert saved["status"] == "VERIFYING" and saved["execution"]["exit_confirmed"]
    assert store.claim() is None
    recovered = Accounts(service).complete(operation["id"], uid())[1]
    assert recovered["status"] == "SUCCEEDED"
    assert recovered["credential_reference"] == "42-" + operation["id"] + ".json"
    assert len(list(database.config.bilibili.state_root.glob("*.json"))) == 1
