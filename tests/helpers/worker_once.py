"""Real crash injector, executed only by recovery tests in a separate process."""

import inspect
import json
import os
import sys
import time
from pathlib import Path

from upload_platform.config import Config
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure.database import Database
from upload_platform.infrastructure.files import write_json
from upload_platform.infrastructure.windows import JobHost, process_creation


def main():
    database = Database(Config.from_env())
    database.initialize()
    store = ExecutionStore(database)
    store.register()
    attempt = store.claim()
    if attempt is None:
        raise RuntimeError("test needs an eligible task")
    point, marker = sys.argv[1:]

    def crash(current):
        if current != point:
            return
        evidence = {"point": current, "attempt_id": attempt["id"]}
        # Capture the test-owned handle identity in the two intentionally unrecorded
        # launch windows, solely to clean up this test's suspended process afterward.
        frame = inspect.currentframe().f_back
        host = frame.f_locals.get("self")
        if current in ("after_create", "after_assign") and host is not None:
            evidence.update(
                pid=host.pid, creation_time=process_creation(host.process), execution=host.execution
            )
        write_json(marker, evidence)
        gate = os.environ.get("UPLOAD_PLATFORM_TEST_CRASH_GATE")
        if gate:
            while not Path(gate).exists():
                time.sleep(0.01)
        os._exit(91)

    if point in ("during_bilibili_send", "after_bilibili_send"):
        send = JobHost.send

        def send_and_crash(host, request):
            if point == "during_bilibili_send":
                message = json.dumps(request)
                host.stdin.write(message[: len(message) // 2])
                host.stdin.flush()
            else:
                send(host, request)
            crash(point)

        JobHost.send = send_and_crash
    Supervisor(store, crash).execute(attempt)


if __name__ == "__main__":
    main()
