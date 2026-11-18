"""GUI maintenance cancellation and actual API-process death windows."""

import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import httpx
import pytest
from sqlalchemy import select, update

from upload_platform.domain.core import uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import stop_verified

# Reuse actual-engine setup, not a protocol mock.
from integration.test_login_sessions import gui as gui, create, wait, post, pytestmark as pytestmark


def test_unstarted_cancel_wins_and_missing_identity_stays_recovering(gui, monkeypatch):
    client, _, app, _ = gui
    sessions = app.state.login_sessions
    kick = sessions.kick
    monkeypatch.setattr(sessions, "kick", lambda _: None)
    identifier = create(client)
    assert post(client, f"/{identifier}/cancel").status_code == 202
    monkeypatch.setattr(sessions, "kick", kick)
    kick(identifier)
    assert wait(client, identifier, lambda x: x["outcome"] is not None)["outcome"] == "cancelled"
    assert "pid" not in sessions.accounts.get(identifier)["execution"]
    monkeypatch.setattr(sessions, "kick", lambda _: None)
    unknown = create(client)
    sessions.accounts.start(unknown, uid())
    with app.state.database.transaction() as conn:
        current = db.get(conn, db.account_maintenance, unknown)
        meta = {**current["execution"]["login_session"], "owner_active": False}
        conn.execute(
            update(db.account_maintenance)
            .where(db.account_maintenance.c.id == unknown)
            .values(execution={**current["execution"], "login_session": meta})
        )
    monkeypatch.setattr(sessions, "kick", kick)
    kick(unknown)
    value = wait(client, unknown, lambda x: x["phase"] == "recovering")
    assert value["outcome"] is None and not value["can_retry"]
    assert post(client).status_code == 409


def test_cancel_racing_reliable_result_returns_success(gui, monkeypatch):
    client, _, app, _ = gui
    sessions = app.state.login_sessions
    original = sessions._finish

    def raced(identifier, generation):
        # The engine has actually published a credential/result and exited.
        with app.state.database.transaction() as conn:
            current = db.get(conn, db.account_maintenance, identifier)
            meta = {**current["execution"]["login_session"], "cancel_requested": True}
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == identifier)
                .values(execution={**current["execution"], "login_session": meta})
            )
        return original(identifier, generation)

    monkeypatch.setattr(sessions, "_finish", raced)
    identifier = create(client)
    assert wait(client, identifier, lambda x: x["outcome"] is not None)["outcome"] == "succeeded"


def test_local_deadline_stops_pending_qr_and_releases_image(gui):
    client, server, app, _ = gui
    server[1]["qr_pending"] = True
    identifier = create(client)
    first = wait(client, identifier, lambda x: x["challenge"] is not None)
    # Advance the local supervisory deadline, not the platform expiry claim.
    app.state.login_sessions._set(identifier, deadline_at=time.time() - 1)
    final = wait(client, identifier, lambda x: x["outcome"] is not None)
    assert final["outcome"] == "expired" and final["error"]["code"] == "login_local_timeout"
    assert client.get(first["challenge"]["image_path"]).status_code == 410


@pytest.mark.parametrize("point", ["qr", "before_commit", "after_commit"])
def test_actual_server_death_recovers_without_new_qr(gui, tmp_path, point):
    _, server, _, base_config = gui
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    data = tmp_path / "crash-core"
    config_file = tmp_path / "engine.json"
    engine = base_config.bilibili
    config_file.write_text(
        json.dumps(
            {
                "executable": str(engine.executable),
                "build_id": engine.build_id,
                "state_root": str(tmp_path / "crash-private"),
                "capabilities_file": str(engine.capabilities_file),
                "allow_test_engine": True,
            }
        )
    )
    env = {
        **os.environ,
        "UPLOAD_PLATFORM_DATA_DIR": str(data),
        "UPLOAD_PLATFORM_API_URL": f"http://127.0.0.1:{port}",
        "UPLOAD_PLATFORM_BILIBILI_CONFIG": str(config_file),
        "GUI_TEST_PORT": str(port),
        "BUE_PROTOCOL_FIXTURE": server[0],
        "GUI_TEST_MARKER": str(tmp_path / "crash-marker"),
        "PYTHONUTF8": "1",
    }
    helper = Path(__file__).parents[1] / "helpers" / "gui_server.py"
    processes, handles = [], []

    def launch(crash=None):
        stream = (tmp_path / f"server-{len(processes)}.log").open("wb")
        child_env = {k: v for k, v in env.items() if k != "GUI_TEST_CRASH"}
        if crash:
            child_env["GUI_TEST_CRASH"] = crash
        process = subprocess.Popen(
            [sys.executable, str(helper)],
            env=child_env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        processes.append((process, stream))
        until = time.monotonic() + 15
        while time.monotonic() < until:
            try:
                if client.get("/api/v1/system/health").status_code == 200:
                    return process
            except httpx.TransportError:
                pass
            if process.poll() is not None:
                raise AssertionError("test server exited before ready")
            time.sleep(0.05)
        raise AssertionError("test server not ready")

    client = httpx.Client(
        base_url=env["UPLOAD_PLATFORM_API_URL"],
        trust_env=False,
        timeout=5,
        headers={"X-Upload-Platform-Client": "gui-bridge-v1"},
    )
    server[1]["qr_pending"] = point == "qr"
    try:
        process = launch(None if point == "qr" else point)
        identifier = create(client)
        if point == "qr":
            first = wait(client, identifier, lambda x: x["challenge"] is not None)
            # Keep independent real OS handles, so this case has verifiable exit evidence.
            import win32api
            import win32job
            from upload_platform.config import Config
            from upload_platform.infrastructure.database import Database

            database = Database(Config(data))
            with database.read() as conn:
                record = db.get(conn, db.account_maintenance, identifier)
            execution = record["execution"]
            handles.extend(
                [
                    win32api.OpenProcess(0x1000 | 0x100000, False, execution["pid"]),
                    win32job.OpenJobObject(0x0004, False, execution["job_name"]),
                ]
            )
            database.close()
            process.terminate()
        process.wait(timeout=20)
        if point != "qr":
            assert process.returncode == 83 and (tmp_path / "crash-marker").exists()
        launch()
        result = wait(client, identifier, lambda x: x["outcome"] is not None)
        assert result["outcome"] == ("failed" if point == "qr" else "succeeded"), result
        assert len([x for x in server[1]["calls"] if "auth_code" in x]) == 1
        if point == "qr":
            assert client.get(first["challenge"]["image_path"]).status_code == 410
        else:
            assert len(list((tmp_path / "crash-private" / "credentials").glob("*.json"))) == 1
    finally:
        client.close()
        for process, stream in processes:
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            stream.close()
        for handle in handles:
            handle.Close()
        # Cleanup only this test's identified engine, including on an assertion failure.
        if data.exists():
            from upload_platform.config import Config
            from upload_platform.infrastructure.database import Database

            database = Database(Config(data))
            with database.read() as conn:
                operations = db.many(conn, select(db.account_maintenance))
            for operation in operations:
                if operation["execution"].get("assigned"):
                    stop_verified(operation["execution"])
            database.close()
