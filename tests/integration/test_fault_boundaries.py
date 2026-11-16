from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import os
import sqlite3
import subprocess
import sys

import pytest
from sqlalchemy import event, select, func
from sqlalchemy.exc import OperationalError

from conftest import save_manifest
from upload_platform.application.contracts import Registration, Submission
from upload_platform.application.service import Service
from upload_platform.config import Config
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import asset_info, prepare


def test_junction_escape_rejected_and_inside_file_allowed(work_factory, tmp_path):
    directory, _ = work_factory()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "foreign.mp4").write_bytes(b"private synthetic fixture")
    junction = directory / "outside-link"
    # Native PowerShell cmdlet; the fixture owns both endpoints and performs no cleanup of targets.
    command = (
        "New-Item -ItemType Junction -Path $env:TEST_JUNCTION -Target $env:TEST_TARGET | Out-Null"
    )
    result = subprocess.run(
        ["pwsh", "-NoProfile", "-Command", command],
        env={**os.environ, "TEST_JUNCTION": str(junction), "TEST_TARGET": str(outside)},
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    with pytest.raises(Problem, match="越出"):
        asset_info(str(directory), "outside-link/foreign.mp4")
    assert asset_info(str(directory), "first.mp4")["size"] > 0
    assert (outside / "foreign.mp4").read_bytes() == b"private synthetic fixture"


def test_initialize_transaction_interruption_leaves_empty_then_recovers(config):
    database = db.Database(config)

    def crash(*_, **__):
        raise RuntimeError("interrupted DDL")

    event.listen(db.works, "after_create", crash)
    try:
        with pytest.raises(RuntimeError):
            database.initialize()
    finally:
        event.remove(db.works, "after_create", crash)
    with sqlite3.connect(config.database) as conn:
        assert conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == []
    database.initialize()
    database.initialize()
    database.close()


def test_process_concurrent_database_initialization(config):
    script = "from upload_platform.config import Config; from upload_platform.infrastructure.database import Database; d=Database(Config.from_env()); d.initialize(); d.close()"
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", script],
            env={**os.environ, "UPLOAD_PLATFORM_DATA_DIR": str(config.data_dir)},
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for _ in range(3)
    ]
    try:
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr.decode(errors="replace")
    finally:
        for process in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=5)
    database = db.Database(config)
    database.initialize()
    with database.read() as conn:
        assert conn.execute(select(func.count()).select_from(db.accounts)).scalar_one() == 1
    database.close()


def test_busy_timeout_is_unavailable_and_does_not_cache_success(database, service, work_factory):
    directory, manifest = work_factory()
    config = Config(database.config.data_dir, busy_timeout_ms=50)
    competitor = db.Database(config)
    key = uid()
    with database.transaction():
        with pytest.raises(OperationalError):
            Service(competitor).registration(
                key, Registration(directory=str(directory), manifest=manifest)
            )
    result = Service(competitor).registration(
        key, Registration(directory=str(directory), manifest=manifest)
    )
    assert result[0] == 201
    competitor.close()


def test_prepare_existing_invalid_id_and_multiple_writers(work_factory):
    directory, manifest = work_factory()
    manifest["work"]["id"] = "invalid"
    save_manifest(directory, manifest)
    before = (directory / "upload.yaml").read_bytes()
    with pytest.raises(Problem):
        prepare(directory)
    assert (directory / "upload.yaml").read_bytes() == before
    del manifest["work"]["id"]
    save_manifest(directory, manifest)

    def write(_):
        try:
            return prepare(directory)["work"]["id"]
        except Problem as exc:
            assert exc.code == "manifest_locked"
            return None

    with ThreadPoolExecutor(4) as pool:
        results = list(pool.map(write, range(4)))
    ids = {result for result in results if result}
    assert len(ids) == 1
    assert prepare(directory)["work"]["id"] in ids


def test_default_normalization_and_preview_identity_changes(service, work_factory):
    directory, manifest = work_factory()
    manifest["work"].pop("description")
    manifest["work"].pop("tags")
    manifest["work"].pop("cover")
    key = uid()
    first = service.registration(key, Registration(directory=str(directory), manifest=manifest))
    explicit = deepcopy(manifest)
    explicit["work"].update(description="", tags=[], cover=None)
    assert (
        service.registration(key, Registration(directory=str(directory), manifest=explicit))
        == first
    )
    save_manifest(directory, manifest)
    work_id = manifest["work"]["id"]
    service.submit(work_id, uid(), Submission())
    assert service.preview(work_id)["expanded"][0]["submission_action"] == "reuse_current"
    manifest["upload_units"][0]["mode"] = "independent"
    save_manifest(directory, manifest)
    preview = service.preview(work_id)
    assert all(item["submission_action"] == "create_new_submission" for item in preview["expanded"])
    assert len(preview["previously_submitted_not_in_preview"]) == 1
