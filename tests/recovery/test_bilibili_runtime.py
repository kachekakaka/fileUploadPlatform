from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sys
import time
import threading

import pytest
from sqlalchemy import insert, select

from conftest import save_manifest
from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.application.contracts import Registration, Submission, Empty
from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db


@pytest.fixture
def setup_bilibili(config, work_factory, tmp_path, monkeypatch):
    build = "core-protocol-v1-fixture"
    capabilities = tmp_path / "capabilities.json"
    capabilities.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "engine_build_id": build,
                "submit_api": "web",
                "upload_ready": True,
                "verified_publish_modes": ["public"],
                "metadata_limits": {
                    "title": 80,
                    "part_title": 80,
                    "description": 2000,
                    "tag": 20,
                    "tag_count": 10,
                    "part_count": 100,
                },
            }
        )
    )
    engine = EngineConfig(
        Path(sys.executable),
        build,
        tmp_path / "private-engine",
        capabilities,
        True,
        (str(Path(__file__).parents[1] / "helpers/bilibili_fixture.py"),),
    )
    configuration = replace(config, bilibili=engine, execution_timeout=0.1)
    database = db.Database(configuration)
    database.initialize()
    with database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(),
                platform="bilibili",
                reference="bilibili:42",
                display_name="fixture",
                status="ACTIVE",
                concurrency_limit=3,
                remote_user_id="42",
                credential_reference="42-00000000-0000-0000-0000-000000000001.json",
            )
        )
    service = Service(database)
    control = tmp_path / "control.json"
    control.write_text("{}")
    monkeypatch.setenv("BUE_FIXTURE_CONTROL", str(control))
    directory, manifest = work_factory()
    manifest["work"]["tags"] = ["test"]
    for video in manifest["assets"]["videos"]:
        video.pop("subtitles", None)
    for unit in manifest["upload_units"]:
        unit["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:42",
                "options": {"category_id": 171, "copyright": 1, "publish_mode": "public"},
            }
        ]
    save_manifest(directory, manifest)
    work_id = manifest["work"]["id"]
    service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    task_id = service.submit(work_id, uid(), Submission())[1]["tasks"][0]["task_id"]
    store = ExecutionStore(database)
    store.register()
    yield database, service, store, task_id, control
    database.close()


def test_bilibili_result_is_real_receipt_with_handoff(setup_bilibili):
    database, service, store, task_id, _ = setup_bilibili
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    task = service.task(task_id)
    assert task["status"] == "SUCCEEDED", task
    with database.read() as conn:
        saved = db.get(conn, db.attempts, attempt["id"])
        receipt = db.one(conn, select(db.receipts).where(db.receipts.c.task_id == task_id))
        assert saved["handed_off_at"] is not None and saved["permission_at"] is None
        assert saved["exit_confirmed"]
        assert receipt["fake"] is False and receipt["evidence"]["aid"] == 123
        assert db.many(conn, select(db.leases).where(db.leases.c.released_at.is_(None))) == []


def test_handoff_rejects_cancel_and_outlives_fake_timeout(setup_bilibili, tmp_path):
    database, service, store, task_id, control = setup_bilibili
    gate = tmp_path / "release"
    control.write_text(json.dumps({"gate": str(gate)}))
    attempt = store.claim()
    with ThreadPoolExecutor() as executor:
        future = executor.submit(Supervisor(store).execute, attempt)
        try:
            received = (
                database.config.data_dir / "executions" / attempt["id"] / "fixture-received.json"
            )
            deadline = time.monotonic() + 15
            while not received.exists() and time.monotonic() < deadline:
                time.sleep(0.01)
            assert received.exists()
            with pytest.raises(Problem) as error:
                service.cancel(task_id, uid(), Empty())
            assert error.value.code == "cancellation_not_supported_after_handoff"
            time.sleep(
                0.3
            )  # Explicitly cross Fake's configured 0.1s limit after the handoff barrier.
            assert not future.done()
            assert service.task(task_id)["cancel_requested_at"] is None
        finally:
            gate.touch()
        future.result(timeout=15)
    assert service.task(task_id)["status"] == "SUCCEEDED"


def test_crash_after_handoff_is_uncertain_not_retry(setup_bilibili):
    _, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({"crash": True}))
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(task_id)["status"] == "UNCERTAIN"
    assert store.claim() is None


def test_cancel_wins_before_handoff_and_sends_no_upload(setup_bilibili):
    database, service, store, task_id, _ = setup_bilibili
    entered, release = threading.Event(), threading.Event()

    def barrier(point):
        if point == "after_identity":
            entered.set()
            assert release.wait(timeout=10)

    attempt = store.claim()
    with ThreadPoolExecutor() as pool:
        future = pool.submit(Supervisor(store, barrier).execute, attempt)
        try:
            assert entered.wait(timeout=10)
            service.cancel(task_id, uid(), Empty())
        finally:
            release.set()
        future.result(timeout=15)
    task = service.task(task_id)
    assert task["status"] == "CANCELLED" and task["attempt"]["handed_off_at"] is None
    assert not (
        database.config.data_dir / "executions" / attempt["id"] / "fixture-received.json"
    ).exists()


@pytest.mark.parametrize("fault", ["wrong_mid", "wrong_build", "conflicting_event", "bad_error"])
def test_invalid_identity_or_result_cannot_become_receipt(setup_bilibili, fault):
    database, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({fault: True, "query_failure": True}))
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(task_id)["status"] == "UNCERTAIN"
    with database.read() as conn:
        assert db.many(conn, select(db.receipts)) == []
        assert db.get(conn, db.attempts, attempt["id"])["exit_confirmed"]
    assert store.claim() is None


def test_stderr_is_drained_without_storing_secret_or_blocking_engine(setup_bilibili):
    database, service, store, task_id, control = setup_bilibili
    control.write_text(json.dumps({"noisy_stderr": True}))
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(task_id)["status"] == "SUCCEEDED"
    log = (database.config.data_dir / "executions" / attempt["id"] / "stderr.log").read_bytes()
    assert len(log) < 256 and b"SYNTHETIC_COOKIE" not in log


def test_multiworker_same_mid_has_three_slots_and_other_mid_is_independent(
    setup_bilibili, work_factory
):
    database, service, _, _, _ = setup_bilibili
    with database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(),
                platform="bilibili",
                reference="bilibili:43",
                display_name="other",
                status="ACTIVE",
                concurrency_limit=3,
                remote_user_id="43",
                credential_reference="43-00000000-0000-0000-0000-000000000001.json",
            )
        )
    for mid in ("42", "42", "42", "43"):
        directory, manifest = work_factory()
        for video in manifest["assets"]["videos"]:
            video.pop("subtitles", None)
        manifest["upload_units"][0]["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:" + mid,
                "options": {"category_id": 171, "copyright": 1, "publish_mode": "public"},
            }
        ]
        save_manifest(directory, manifest)
        service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
        service.submit(manifest["work"]["id"], uid(), Submission())
    workers = [ExecutionStore(database) for _ in range(6)]
    for worker in workers:
        worker.register()
    with ThreadPoolExecutor(max_workers=6) as pool:
        claimed = [result for result in pool.map(lambda worker: worker.claim(), workers) if result]
    mids = [
        service.task(attempt["task_id"])["snapshot"]["target"]["expected_mid"]
        for attempt in claimed
    ]
    assert mids.count("42") == 3 and mids.count("43") == 1
    assert all(worker.claim() is None for worker in workers)
