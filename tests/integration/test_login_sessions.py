"""GUI-facing Core contract exercised with the real Rust engine, never real credentials."""

import base64
from dataclasses import replace
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import time

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import select, update

from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.api.app import create_app
from upload_platform.application.login_sessions import validate_png
from upload_platform.domain.core import uid
from upload_platform.infrastructure import database as db


pytestmark = pytest.mark.skipif(
    not os.getenv("BUE_TEST_EXECUTABLE") or not os.getenv("BUE_HTTP_TEST_MODULE"),
    reason="requires actual fixture Rust engine and local HTTP fixture module",
)


@pytest.fixture
def gui(config, tmp_path, monkeypatch):
    executable = Path(os.environ["BUE_TEST_EXECUTABLE"])
    caps = json.loads(subprocess.check_output([str(executable), "capabilities"], text=True))
    assert caps["engine_build_id"].endswith("-fixture")
    capfile = tmp_path / "capabilities.json"
    capfile.write_text(json.dumps(caps))
    engine = EngineConfig(executable, caps["engine_build_id"], tmp_path / "private", capfile, True)
    config = replace(config, bilibili=engine)
    spec = importlib.util.spec_from_file_location(
        "gui_http_fixture", os.environ["BUE_HTTP_TEST_MODULE"]
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    fixture = module.server.__wrapped__()
    server = next(fixture)
    monkeypatch.setenv("BUE_PROTOCOL_FIXTURE", server[0])
    app = create_app(config)
    try:
        with TestClient(
            app, base_url=config.api_url, headers={"X-Upload-Platform-Client": "gui-bridge-v1"}
        ) as client:
            yield client, server, app, config
    finally:
        fixture.close()


def post(client, path="", body=None, key=None):
    return client.post(
        "/api/v1/account-login-sessions" + path,
        json=body or {},
        headers={"Idempotency-Key": key or uid()},
    )


def wait(client, identifier, predicate, timeout=25):
    until = time.monotonic() + timeout
    while time.monotonic() < until:
        response = client.get("/api/v1/account-login-sessions/" + identifier)
        assert response.status_code == 200, response.text
        value = response.json()
        if predicate(value):
            return value
        time.sleep(0.05)
    raise AssertionError(value)


def create(client, **body):
    response = post(client, body=body)
    assert response.status_code == 202, response.text
    return response.json()["session_id"]


def test_gui_scan_without_worker_idempotency_safe_png_and_summary(gui):
    client, server, app, config = gui
    server[1]["qr_pending"] = True
    assert client.get("/api/v1/system/health").json()["workers"] == []
    assert client.get("/api/v1/system/capabilities").json()["gui_login"]["available"]
    key = uid()
    first = post(client, key=key)
    identifier = first.json()["session_id"]
    assert post(client, key=key).json()["session_id"] == identifier
    assert post(client).status_code == 409
    value = wait(client, identifier, lambda x: x["challenge"] is not None)
    image_path = value["challenge"]["image_path"]
    image = client.get(image_path)
    assert image.status_code == 200 and image.headers["content-type"] == "image/png"
    assert image.headers["cache-control"] == "no-store"
    assert image.headers["x-content-type-options"] == "nosniff"
    assert validate_png(base64.b64encode(image.content)) == image.content
    assert server[2] not in json.dumps(value)
    assert not any(
        k in json.dumps(value)
        for k in ("credential_reference", "state_root", "engine_request", "png_base64")
    )
    raw = config.database.read_bytes()
    assert b"png_base64" not in raw and b"synthetic-qr" not in raw
    assert image.content not in raw
    assert client.get("/api/v1/accounts").status_code == 403
    assert (
        client.get("/api/v1/account-login-sessions").json()["items"][0]["session_id"] == identifier
    )
    for suffix in ("start", "complete", "identified"):
        blocked = client.post(
            f"/api/v1/account-maintenance/{identifier}/{suffix}",
            json={},
            headers={"X-Upload-Platform-Client": "cli-v1", "Idempotency-Key": uid()},
        )
        assert blocked.status_code == 409
    server[1]["qr_pending"] = False
    done = wait(client, identifier, lambda x: x["outcome"] is not None)
    assert done["outcome"] == "succeeded", done
    assert done["account_summary"]["remote_user_id"] == "42"
    assert client.get(image_path).status_code == 410
    assert client.get("/api/v1/account-login-sessions").json() == {"items": []}
    summary = client.get("/api/v1/accounts?view=summary").json()
    assert "credential_reference" not in json.dumps(summary)
    assert len([x for x in server[1]["calls"] if "auth_code" in x]) == 1
    assert app.state.login_sessions.accounts.get(identifier)["execution"]["exit_confirmed"]


def test_cancel_and_explicit_new_challenge(gui):
    client, server, app, _ = gui
    server[1]["qr_pending"] = True
    identifier = create(client)
    value = wait(client, identifier, lambda x: x["challenge"] is not None)
    key = uid()
    assert post(client, f"/{identifier}/cancel", key=key).status_code in (200, 202)
    assert post(client, f"/{identifier}/cancel", key=key).status_code in (200, 202)
    done = wait(client, identifier, lambda x: x["outcome"] is not None)
    assert done["outcome"] == "cancelled"
    assert client.get(value["challenge"]["image_path"]).status_code == 410
    server[1]["qr_pending"] = False
    another = create(client)
    assert another != identifier
    assert wait(client, another, lambda x: x["outcome"] is not None)["outcome"] == "succeeded"


def test_relogin_same_mid_preserves_disabled_and_old_reference_on_failure(gui):
    client, server, app, _ = gui
    identifier = create(client)
    done = wait(client, identifier, lambda x: x["outcome"] is not None)
    account_id = done["account_id"]
    with app.state.database.transaction() as conn:
        account = db.get(conn, db.accounts, account_id)
        conn.execute(
            update(db.accounts).where(db.accounts.c.id == account_id).values(enabled=False)
        )
    server[1]["qr_poll_code"] = -999
    failed = create(client, account_id=account_id)
    failure = wait(client, failed, lambda x: x["outcome"] is not None)
    assert failure["outcome"] == "failed"
    assert failure["error"]["code"] == "LOGIN_PLATFORM_ERROR"
    with app.state.database.read() as conn:
        assert (
            db.get(conn, db.accounts, account_id)["credential_reference"]
            == account["credential_reference"]
        )
        assert db.many(conn, select(db.account_blocks)) == []
    server[1]["qr_poll_code"] = None
    again = create(client)  # Re-register the existing MID, with server-owned identified handshake.
    result = wait(client, again, lambda x: x["outcome"] is not None)
    assert result["outcome"] == "succeeded" and result["account_id"] == account_id
    assert result["account_summary"]["availability"] == "disabled"
    with app.state.database.read() as conn:
        assert (
            len(db.many(conn, select(db.accounts).where(db.accounts.c.platform == "bilibili"))) == 1
        )


def test_core_commit_failure_resumes_existing_published_version(gui):
    client, server, app, config = gui
    sessions = app.state.login_sessions
    failed = []

    def fault(point):
        if point == "before_commit" and not failed:
            with app.state.database.read() as conn:
                rows = db.many(conn, select(db.account_maintenance))
            if any(
                r["execution"].get("login_session", {}).get("phase") == "finalizing" for r in rows
            ):
                failed.append(True)
                raise RuntimeError("injected commit rollback")

    app.state.service.fault = fault
    identifier = create(client)
    value = wait(client, identifier, lambda x: x["phase"] == "recovering")
    assert value["outcome"] is None and failed
    with app.state.database.read() as conn:
        assert not db.many(conn, select(db.accounts).where(db.accounts.c.platform == "bilibili"))
    versions = list((config.bilibili.state_root / "credentials").glob("*.json"))
    assert len(versions) == 1
    # Wait for the old coordinator to relinquish ownership before explicit resume.
    until = time.monotonic() + 5
    while identifier in sessions.threads and time.monotonic() < until:
        time.sleep(0.02)
    assert post(client, f"/{identifier}/resume").status_code == 202
    assert wait(client, identifier, lambda x: x["outcome"] is not None)["outcome"] == "succeeded"
    assert list((config.bilibili.state_root / "credentials").glob("*.json")) == versions
    assert len([x for x in server[1]["calls"] if "auth_code" in x]) == 1


def test_browser_source_rejected_and_legacy_engine_refused(gui):
    client, _, _, config = gui
    for header in ({"Origin": "http://localhost"}, {"Sec-Fetch-Site": "same-origin"}):
        assert client.get("/api/v1/account-login-sessions", headers=header).status_code == 403
    assert post(client, body={"import_file": "C:/private.json"}).status_code == 422
    caps = json.loads(config.bilibili.capabilities_file.read_text())
    del caps["login_interaction"]
    config.bilibili.capabilities_file.write_text(json.dumps(caps))
    assert not client.get("/api/v1/system/capabilities").json()["gui_login"]["available"]
    assert post(client).status_code == 409


@pytest.mark.parametrize("corruption", ["identity", "extra_field", "png", "oversized"])
def test_malformed_machine_messages_do_not_escape_into_storage(gui, monkeypatch, corruption):
    client, server, app, config = gui
    server[1]["qr_pending"] = True
    sessions = app.state.login_sessions
    original = sessions._consume
    marker = "SENSITIVE-INJECTED-PROTOCOL-CONTENT"

    def corrupt(identifier, generation, line, host, last_seq):
        value = json.loads(line)
        if value.get("type") == "login_qr_ready":
            if corruption == "identity":
                value["operation_id"] = uid()
            elif corruption == "extra_field":
                value["cookie"] = marker
            elif corruption == "png":
                value["png_base64"] = base64.b64encode(marker.encode()).decode()
            else:
                value["png_base64"] = "A" * 180000
            line = json.dumps(value)
        return original(identifier, generation, line, host, last_seq)

    monkeypatch.setattr(sessions, "_consume", corrupt)
    identifier = create(client)
    result = wait(client, identifier, lambda x: x["outcome"] is not None)
    assert result["outcome"] == "failed" and result["challenge"] is None
    for path in config.data_dir.rglob("*"):
        if path.is_file():
            assert marker.encode() not in path.read_bytes()


def test_cancel_while_waiting_for_upload_does_not_launch_or_stop_upload(gui, work_factory):
    from conftest import save_manifest
    from upload_platform.application.contracts import Registration, Submission
    from upload_platform.execution.leases import ExecutionStore

    client, server, app, _ = gui
    initial = create(client)
    account = wait(client, initial, lambda x: x["outcome"] is not None)["account_id"]
    directory, manifest = work_factory()
    manifest["work"].pop("cover", None)
    for video in manifest["assets"]["videos"]:
        video.pop("subtitles", None)
    manifest["upload_units"][0]["targets"] = [
        {
            "platform": "bilibili",
            "account": "bilibili:42",
            "options": {"category_id": 171, "copyright": 1, "publish_mode": "public"},
        }
    ]
    save_manifest(directory, manifest)
    app.state.service.registration(uid(), Registration(directory=str(directory), manifest=manifest))
    app.state.service.submit(manifest["work"]["id"], uid(), Submission())
    store = ExecutionStore(app.state.database)
    store.register()
    attempt = store.claim()
    assert attempt
    identifier = create(client, account_id=account)
    value = wait(client, identifier, lambda x: x["phase"] == "waiting_for_executions")
    assert value["challenge"] is None
    conflict = client.post(
        "/api/v1/account-maintenance",
        json={"kind": "renew", "account_id": account},
        headers={"X-Upload-Platform-Client": "cli-v1", "Idempotency-Key": uid()},
    )
    assert conflict.status_code == 409
    assert post(client, f"/{identifier}/cancel").status_code == 202
    done = wait(client, identifier, lambda x: x["outcome"] is not None)
    assert done["outcome"] == "cancelled"
    assert len([x for x in server[1]["calls"] if "auth_code" in x]) == 1
    with app.state.database.read() as conn:
        assert (
            db.one(conn, select(db.leases).where(db.leases.c.attempt_id == attempt["id"]))[
                "released_at"
            ]
            is None
        )
        assert not db.many(conn, select(db.account_blocks))
    store.finish(attempt, "permanent", proof="no_execution_intent")


@pytest.mark.parametrize("same_key", [True, False])
def test_concurrent_creation_never_spawns_duplicate_login(gui, same_key):
    from concurrent.futures import ThreadPoolExecutor

    client, server, _, _ = gui
    server[1]["qr_pending"] = True
    key = uid()
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: post(client, key=key if same_key else uid()), range(4)))
    accepted = [r for r in replies if r.status_code == 202]
    assert len(accepted) == (4 if same_key else 1)
    assert all(r.status_code in (202, 409) for r in replies)
    identifiers = {r.json()["session_id"] for r in accepted}
    assert len(identifiers) == 1
    identifier = identifiers.pop()
    wait(client, identifier, lambda x: x["challenge"] is not None)
    assert len([x for x in server[1]["calls"] if "auth_code" in x]) == 1
    post(client, f"/{identifier}/cancel")
    assert wait(client, identifier, lambda x: x["outcome"] is not None)["outcome"] == "cancelled"
    if same_key:
        replay = post(client, key=key)
        assert replay.json()["session_id"] == identifier
        assert replay.json()["outcome"] == "cancelled"
