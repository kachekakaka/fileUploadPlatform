from contextlib import closing
import json
import os
from pathlib import Path
import socket
import sqlite3
from uuid import uuid4
from zipfile import ZipFile

import httpx
import pytest

from test_cli_server_worker import wait_until

pytestmark = pytest.mark.skipif(
    os.name != "nt" or not os.getenv("UPLOAD_PLATFORM_TEST_PORTABLE_ZIP"),
    reason="supply the final portable Windows ZIP on an interactive Windows desktop",
)


@pytest.fixture
def portable_case(tmp_path, monkeypatch):
    from upload_platform.portable import Portable

    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", 8765))
        except OSError:
            pytest.skip("the existing deployment owns TCP 8765")
    with ZipFile(os.environ["UPLOAD_PLATFORM_TEST_PORTABLE_ZIP"]) as archive:
        assert archive.testzip() is None
        assert not any(
            name.lower().endswith((".mp4", ".sqlite3", ".log")) for name in archive.namelist()
        )
        archive.extractall(tmp_path / "全新 解压目录")
    (bundle,) = (tmp_path / "全新 解压目录").iterdir()
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "用户设置"))
    windir = Path(os.environ["SystemRoot"])
    monkeypatch.setenv("PATH", os.pathsep.join([str(windir / "System32"), str(windir)]))
    recordings = tmp_path / "录像 原件"
    recordings.mkdir()
    platform = Portable.for_user(bundle)
    yield platform, recordings
    if platform.settings() and platform.settings()["deployment_id"]:
        platform.stop(lambda role: None)


def test_double_click_setup_reuses_deployment_and_drains(portable_case, work_factory):
    import win32con as c
    import win32gui as gui

    platform, recordings = portable_case
    (recordings / "sessions").mkdir()

    def children(hwnd, kind):
        found = []

        def visit(handle, _):
            if gui.GetClassName(handle) == kind:
                found.append(handle)
            return True

        gui.EnumChildWindows(hwnd, visit, None)
        return found

    def text(hwnd, label):
        return any(label in gui.GetWindowText(handle) for handle in children(hwnd, "Static"))

    def windows():
        found = set()

        def visit(hwnd, _):
            if gui.GetClassName(hwnd).lower() == "fileuploadplatformportable":
                found.add(hwnd)
            return True

        gui.EnumWindows(visit, None)
        return found

    def launch():
        old = windows()
        os.startfile(platform.bundle / "启动上传平台.cmd")
        return wait_until(lambda: next(iter(windows() - old), None), timeout=35)

    def click(hwnd, button_id):
        button = gui.GetDlgItem(hwnd, button_id)
        gui.PostMessage(hwnd, c.WM_COMMAND, button_id, button)

    first = launch()
    try:
        edits = children(first, "Edit")
        assert len(edits) == 2
        gui.SendMessage(edits[0], c.WM_SETTEXT, 0, str(recordings))
        click(first, 103)
        wait_until(lambda: text(first, "MuMu 可以连接"), timeout=50)
        deployment = platform.settings()["deployment_id"]
        server = platform.record("server", deployment)
        worker = platform.record("worker", deployment)
        first_beat = next(
            row["heartbeat_at"] for row in platform.health(deployment)["workers"] if row["active"]
        )
        second = launch()
        try:
            wait_until(lambda: text(second, "MuMu 可以连接"), timeout=45)
            assert platform.record("server", deployment) == server
            assert platform.record("worker", deployment) == worker
            wait_until(
                lambda: any(
                    row["active"] and row["heartbeat_at"] > first_beat
                    for row in platform.health(deployment)["workers"]
                ),
                timeout=23,
            )
            directory, manifest = work_factory(
                directory=recordings / "sessions" / "fixture", duration=8
            )
            with httpx.Client(base_url="http://127.0.0.1:8765/api/v1", trust_env=False) as client:

                def post(path, body):
                    result = client.post(
                        path,
                        json=body,
                        headers={
                            "X-Upload-Platform-Client": "cli-v1",
                            "Idempotency-Key": str(uuid4()),
                        },
                    )
                    assert result.status_code == 201, result.text
                    return result.json()

                work = post("/works", {"directory": str(directory), "manifest": manifest})
                (task,) = post(f"/works/{work['work_id']}/submit", {})["tasks"]
                task_id = task["task_id"]
                wait_until(lambda: client.get(f"/tasks/{task_id}").json()["status"] == "RUNNING")
        finally:
            gui.PostMessage(second, c.WM_CLOSE, 0, 0)
        assert platform.health(deployment)
        click(first, 104)
        wait_until(lambda: text(first, "上传平台已退出"), timeout=35)
        assert platform.health(deployment) is None
        click(first, 103)
        wait_until(lambda: text(first, "MuMu 可以连接"), timeout=50)
        assert platform.settings()["deployment_id"] == deployment
        assert platform.record("server", deployment)["token"] != server["token"]
        assert platform.record("worker", deployment)["token"] != worker["token"]
        original = platform.diagnostics(f"tasks/{task_id}")
        assert original["status"] == "SUCCEEDED" and original["attempt_count"] == 1
    finally:
        gui.PostMessage(first, c.WM_CLOSE, 0, 0)


def test_startup_faults_preserve_data_and_port_owner(portable_case):
    from upload_platform.domain.core import Problem

    platform, recordings = portable_case

    def start():
        return platform.start(str(recordings), str(platform.default_data), lambda *_: None)

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 8765))
        listener.listen()
        with pytest.raises(Problem, match="8765"):
            start()
        assert listener.getsockname()[1] == 8765
        assert not (platform.default_data / "core/core.sqlite3").exists()

    build = json.loads((platform.bundle / "release.json").read_text())["engine_build_id"]
    caps = platform.bundle / "engines" / build / "capabilities.json"
    original = caps.read_bytes()
    try:
        caps.write_text('{"engine_build_id": "other"}', encoding="utf-8")
        with pytest.raises(Problem, match="引擎"):
            start()
        caps.unlink()
        with pytest.raises(Problem, match="引擎"):
            start()
    finally:
        caps.write_bytes(original)

    database = platform.default_data / "core/core.sqlite3"
    database.parent.mkdir()
    with closing(sqlite3.connect(database)) as conn, conn:
        conn.execute("CREATE TABLE schema_version (id, version, deployment_id)")
        conn.execute("INSERT INTO schema_version VALUES ('core', 1, NULL)")
    original_db = database.read_bytes()
    with pytest.raises(Problem, match="原数据库"):
        start()
    assert database.read_bytes() == original_db
    database.rename(database.with_suffix(".old"))

    worker_log = platform.default_data / "logs/worker.log"
    worker_log.mkdir(parents=True)
    with pytest.raises(Problem, match="worker"):
        start()
    deployment = platform.settings()["deployment_id"]
    assert platform.record("server", deployment) is None
    assert platform.health(deployment) is None
    worker_log.rmdir()
    assert start()[0] == build
    platform.stop(lambda role: None)
    database.rename(database.with_suffix(".saved"))
    with pytest.raises(Problem, match="原数据库丢失"):
        start()
    assert not database.exists()
