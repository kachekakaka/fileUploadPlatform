import json
import os
import queue
import subprocess
import sys
import threading
import time

from upload_platform.execution.protocol import HostRequest, Command
from upload_platform.infrastructure.files import write_json


def run():
    request = HostRequest.model_validate_json(sys.stdin.readline())
    ident = {
        k: getattr(request, k)
        for k in ("protocol_version", "task_id", "attempt_id", "execution_id", "generation")
    }
    options = request.options
    sequence = 0
    history = []

    def emit(kind, **values):
        nonlocal sequence
        sequence += 1
        event = {**ident, "type": kind, "host_sequence": sequence, **values}
        history.append(event)
        sys.stdout.write(json.dumps(event) + "\n")
        sys.stdout.flush()

    messages = queue.Queue()

    def reader():
        for line in sys.stdin:
            try:
                command = Command.model_validate_json(line)
                if all(getattr(command, k) == v for k, v in ident.items()):
                    messages.put(command.type)
            except Exception:
                continue

    threading.Thread(target=reader, daemon=True).start()
    if options.scenario == "no_handshake":
        time.sleep(600)
    if options.scenario == "crash_before_go":
        os._exit(72)
    emit("hello", phase="waiting_permission")
    try:
        permission = messages.get(timeout=30)
    except queue.Empty:
        return
    if permission != "go":
        return
    emit("progress", phase="remote", progress=0)
    if options.scenario == "child":
        child = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        emit("progress", phase="remote", progress=1, child_pid=child.pid)
    started = time.monotonic()
    cancelled = False
    duration = 600 if options.scenario in ("hang", "child") else options.duration_seconds
    while time.monotonic() - started < duration:
        try:
            if messages.get(timeout=min(0.05, max(0.001, duration))) == "cancel":
                if options.scenario not in ("hang", "late_result"):
                    cancelled = True
                    break
        except queue.Empty:
            pass
    outcome, safe = "succeeded", True
    if cancelled:
        outcome = "uncertain" if options.scenario == "cancel_uncertain" else "cancelled"
        safe = outcome == "cancelled"
    elif options.scenario == "retryable" and request.attempt_number <= max(
        1, options.failures_before_success
    ):
        outcome = "retryable"
    elif options.scenario == "permanent":
        outcome = "permanent"
    observation = "failed" if outcome != "succeeded" else "succeeded"
    write_json(
        request.evidence_path,
        {"fake": True, "execution_id": request.execution_id, "outcome": observation},
    )
    if options.scenario == "crash":
        os._exit(73)
    emit("progress", phase="finished", progress=100)
    sequence += 1
    result = {
        **ident,
        "type": "result",
        "host_sequence": sequence,
        "history": history,
        "outcome": outcome,
        "safe": safe,
        "fake": True,
        "receipt": {
            "fake": True,
            "execution_id": request.execution_id,
            "remote_id": "fake-" + request.attempt_id,
        }
        if outcome == "succeeded"
        else None,
    }
    if options.scenario == "invalid_result":
        result["execution_id"] = "wrong-execution"
    write_json(request.result_path, result)
    sys.stdout.write(
        json.dumps(
            {
                **ident,
                "type": "result",
                "host_sequence": sequence,
                "result_path": request.result_path,
            }
        )
        + "\n"
    )
    sys.stdout.flush()


if __name__ == "__main__":
    try:
        run()
    except Exception as exc:
        # No raw request, filesystem content, or arbitrary exception message in logs.
        print("Fake host error: " + type(exc).__name__, file=sys.stderr)
        sys.exit(74)
