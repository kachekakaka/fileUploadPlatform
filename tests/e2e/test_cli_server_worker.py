from datetime import datetime, timedelta, timezone
import json
import os
import socket
import subprocess
import sys
import time

import httpx
import pytest

from conftest import save_manifest


def wait_until(fn, timeout=45):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        try:
            last = fn()
            if last:
                return last
        except (httpx.HTTPError, KeyError):
            pass
        time.sleep(0.1)
    raise AssertionError(f"end-to-end condition timed out; last={last}")


class Environment:
    def __init__(self, directory):
        self.directory = directory
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        self.url = f"http://127.0.0.1:{port}"
        self.env = {
            **os.environ,
            "PYTHONUTF8": "1",
            "UPLOAD_PLATFORM_DATA_DIR": str(directory / "data"),
            "UPLOAD_PLATFORM_API_URL": self.url,
            "UPLOAD_PLATFORM_HEARTBEAT_SECONDS": "0.3",
            "UPLOAD_PLATFORM_LEASE_SECONDS": "3",
            "UPLOAD_PLATFORM_POLL_SECONDS": "0.05",
        }
        self.processes = []
        self.http = httpx.Client(
            base_url=self.url + "/api/v1",
            trust_env=False,
            follow_redirects=False,
            timeout=2,
            headers={"X-Upload-Platform-Client": "cli-v1"},
        )
        self.commands = []

    def start(self, role):
        stream = open(self.directory / f"{role}-{len(self.processes)}.log", "w", encoding="utf-8")
        process = subprocess.Popen(
            [sys.executable, "-m", "upload_platform", role],
            env=self.env,
            stdout=stream,
            stderr=subprocess.STDOUT,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.processes.append((process, stream))
        return process

    def cli(self, *args, success=True):
        result = subprocess.run(
            [sys.executable, "-m", "upload_platform", "--json", *args],
            env=self.env,
            capture_output=True,
            encoding="utf-8",
            timeout=25,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        self.commands.append(
            {
                "args": args,
                "exit_code": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
            }
        )
        if success:
            assert result.returncode == 0, self.commands[-1]
            return json.loads(result.stdout)
        return result

    def close(self):
        for process, stream in reversed(self.processes):
            if process.poll() is None:
                process.terminate()
                process.wait(timeout=10)
            stream.close()
        self.http.close()
        (self.directory / "commands.json").write_text(
            json.dumps(self.commands, ensure_ascii=False, indent=2), encoding="utf-8"
        )


@pytest.mark.parametrize(
    "scenario,expected", [("child", "CANCELLED"), ("late_result", "SUCCEEDED")]
)
def test_cli_running_cancel_and_late_success(tmp_path, work_factory, scenario, expected):
    import win32api
    import win32event
    from upload_platform.infrastructure.windows import stop_verified

    directory, _ = work_factory(scenario=scenario, duration=6)
    environment = Environment(tmp_path)
    handles = []
    execution = None
    try:
        server = environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        work_id = environment.cli("work", "register", "--dir", str(directory))["operation_result"][
            "work_id"
        ]
        task_id = environment.cli("work", "upload", work_id)["operation_result"]["tasks"][0][
            "task_id"
        ]
        environment.start("worker")
        wait_until(lambda: environment.http.get("/tasks/" + task_id).json()["status"] == "RUNNING")
        task = environment.http.get("/tasks/" + task_id).json()
        execution = task["attempt"]["execution"]
        handles.append(win32api.OpenProcess(0x00100000, False, execution["pid"]))
        if scenario == "child":
            child_pid = wait_until(
                lambda: next(
                    (
                        e["payload"]["child_pid"]
                        for e in environment.http.get(f"/tasks/{task_id}/events").json()["items"]
                        if e["payload"].get("child_pid")
                    ),
                    None,
                )
            )
            handles.append(win32api.OpenProcess(0x00100000, False, child_pid))
        cancelled = environment.cli("task", "cancel", task_id)
        assert cancelled["operation_result"]["status"] == "RUNNING"
        wait_until(lambda: environment.http.get("/tasks/" + task_id).json()["status"] == expected)
        result = environment.cli("task", "status", task_id)
        assert result["attempt"]["exit_confirmed"]
        assert all(
            win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_OBJECT_0
            for handle in handles
        )
        events = environment.cli("task", "events", task_id)["items"]
        assert sum(e["type"] == "attempt.finished" for e in events) == 1
        assert any(e["type"] == "task.cancel_not_effective" for e in events) == (
            expected == "SUCCEEDED"
        )
        assert bool(result["receipt"]) == (expected == "SUCCEEDED")
        assert len(environment.cli("task", "attempts", task_id)["items"]) == 1
        environment.cli("request", "resume", cancelled["request_id"])
        server.terminate()
        server.wait(timeout=5)
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        assert environment.cli("task", "status", task_id)["status"] == expected
    finally:
        if execution and any(
            win32event.WaitForSingleObject(handle, 0) == win32event.WAIT_TIMEOUT
            for handle in handles
        ):
            stop_verified(execution)
        for handle in handles:
            handle.Close()
        environment.close()


def test_cli_register_preview_submit_execution_restart_and_request_replay(tmp_path, work_factory):
    directory, manifest = work_factory()
    independent = {
        "id": "clips",
        "mode": "independent",
        "videos": ["v1", "v2"],
        "targets": [{"platform": "fake", "account": "fake-default"}],
    }
    manifest["upload_units"].append(independent)
    del manifest["work"]["id"]
    manifest["work"]["ready"] = False
    save_manifest(directory, manifest)
    environment = Environment(tmp_path)
    try:
        server = environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        first = environment.cli("work", "prepare", "--dir", str(directory))
        second = environment.cli("work", "prepare", "--dir", str(directory))
        assert first["work"]["id"] == second["work"]["id"]
        assert first["work"]["ready"] is False
        environment.cli("work", "ready", "--dir", str(directory))
        registered = environment.cli(
            "work", "register", "--manifest", str(directory / "upload.yaml")
        )
        work_id = registered["operation_result"]["work_id"]
        assert environment.cli("work", "show", work_id)["status"] == "NOT_SUBMITTED"
        assert len(environment.cli("work", "preview", work_id)["expanded"]) == 3
        submitted = environment.cli("work", "upload", work_id)
        task_ids = [task["task_id"] for task in submitted["operation_result"]["tasks"]]
        assert len(task_ids) == 3
        cancelled = environment.cli("task", "cancel", task_ids[0])
        assert cancelled["operation_result"]["status"] == "CANCELLED"
        resubmitted = environment.cli("task", "reupload", task_ids[0])
        replacement = resubmitted["operation_result"]["tasks"][0]["task_id"]
        current_ids = [replacement, *task_ids[1:]]
        assert environment.cli("doctor")["workers"] == []
        worker = environment.start("worker")
        wait_until(
            lambda: all(
                environment.http.get("/tasks/" + task_id).json()["status"] == "SUCCEEDED"
                for task_id in current_ids
            )
        )
        for task_id in current_ids:
            task = environment.cli("task", "status", task_id)
            assert task["receipt"]["fake"] and task["attempt"]["exit_confirmed"]
        work_status = environment.cli("work", "status", work_id)
        assert work_status["work"]["status"] == "SUCCEEDED"
        assert len(work_status["tasks"]["items"]) == 4
        events = environment.cli("task", "events", replacement)["items"]
        assert any(event["type"] == "host.progress" for event in events)
        assert len(environment.cli("task", "attempts", replacement)["items"]) == 1
        # SSE reconnect is over a real socket, with exactly the same durable sequence.
        for after in (0, 1):
            with environment.http.stream(
                "GET",
                f"/tasks/{replacement}/events/stream",
                headers={"Last-Event-ID": str(after)},
                timeout=5,
            ) as stream:
                assert stream.status_code == 200
                event = next(
                    json.loads(line[6:])
                    for line in stream.iter_lines()
                    if line.startswith("data: ")
                )
                assert event["sequence"] == after + 1
        # Move source manifest after success; replay uses the saved original request.
        (directory / "upload.yaml").rename(directory / "moved.yaml")
        resumed = environment.cli("request", "resume", submitted["request_id"])
        assert resumed["operation_result"] == submitted["operation_result"]
        assert len(resumed["current_tasks"]) == 3
        worker.terminate()
        worker.wait(timeout=5)
        server.terminate()
        server.wait(timeout=5)
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        assert environment.cli("work", "show", work_id)["status"] == "SUCCEEDED"
        assert environment.cli("task", "status", task_ids[0])["status"] == "CANCELLED"
        assert environment.cli("task", "status", replacement)["previous_task_id"] == task_ids[0]
        assert len(list(directory.glob("*.mp4"))) == 2
        assert not list((tmp_path / "data").rglob("*.mp4"))
        time.sleep(3.1)
        assert all(not w["active"] for w in environment.cli("doctor")["workers"])
    finally:
        environment.close()


def test_cli_daily_weekly_rules_shared_tasks_and_versioned_management(tmp_path, work_factory):
    parent = tmp_path / "parent"
    _, manifest = work_factory(directory=parent / "work")
    environment = Environment(tmp_path)
    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        fire = datetime.now(timezone.utc) + timedelta(seconds=12)
        local_time = fire.strftime("%H:%M:%S")
        daily = environment.cli(
            "schedule",
            "create",
            "--dir",
            str(parent),
            "--kind",
            "daily",
            "--time",
            local_time,
            "--timezone",
            "UTC",
        )["operation_result"]
        weekly = environment.cli(
            "schedule",
            "create",
            "--dir",
            str(parent),
            "--kind",
            "weekly",
            "--weekday",
            str(fire.isoweekday()),
            "--time",
            local_time,
            "--timezone",
            "UTC",
        )["operation_result"]
        assert len(environment.cli("schedule", "list")["items"]) == 2
        environment.start("worker")
        wait_until(
            lambda: (
                len(environment.http.get(f"/schedules/{daily['id']}/runs").json()["items"]) == 1
                and len(environment.http.get(f"/schedules/{weekly['id']}/runs").json()["items"])
                == 1
            )
        )
        wait_until(
            lambda: (
                environment.http.get("/works/" + manifest["work"]["id"]).json().get("status")
                == "SUCCEEDED"
            )
        )
        assert len(environment.cli("task", "list")["items"]) == 1
        runs = environment.cli("schedule", "runs", daily["id"])["items"]
        run = environment.cli("schedule", "run", runs[0]["id"])
        assert run["status"] == "COMPLETED" and len(run["tasks"]) == 1
        assert run["tasks"][0]["status"] == "SUCCEEDED"
        disabled = environment.cli("schedule", "disable", daily["id"])["operation_result"]
        assert disabled["enabled"] is False
        enabled = environment.cli("schedule", "enable", daily["id"])["operation_result"]
        assert enabled["enabled"] is True and enabled["version"] == 3
        updated = environment.cli(
            "schedule",
            "update",
            daily["id"],
            "--time",
            "23:59:59",
            "--max-attempts",
            "1",
            "--max-lateness-seconds",
            "10",
        )["operation_result"]
        assert updated["config"]["retry_policy"] == {"max_attempts": 1, "retry_delays_seconds": []}
        assert updated["config"]["max_lateness_seconds"] == 10
        assert len(environment.cli("schedule", "show", daily["id"])["versions"]) == 4
    finally:
        environment.close()


def test_cli_unreachable_keeps_saved_request_and_never_writes_database(tmp_path):
    environment = Environment(tmp_path)
    try:
        failed = environment.cli(
            "task", "cancel", "00000000-0000-0000-0000-000000000001", success=False
        )
        assert failed.returncode == 3
        failure = json.loads(failed.stderr)
        request_id = failure["details"]["request_id"]
        log = tmp_path / "data" / "requests" / (request_id + ".json")
        assert json.loads(log.read_text(encoding="utf-8"))["state"] == "pending"
        assert not (tmp_path / "data" / "core.sqlite3").exists()
    finally:
        environment.close()
