import sys
import time
import json
import queue

import httpx

from upload_platform.domain.core import Problem
from upload_platform.infrastructure.windows import JobHost


def maintain(client, kind, account_id=None, operation_id=None, import_file=None):
    client.http.timeout = httpx.Timeout(120)
    if operation_id is None:
        operation = client.mutate(
            "POST",
            "/account-maintenance",
            {"kind": kind, "account_id": account_id, "import_file": import_file},
        )["operation_result"]
        operation_id = operation["id"]
    print("账号维护操作：" + operation_id, file=sys.stderr, flush=True)
    route = "/account-maintenance/" + operation_id
    operation = client.read(route)
    if operation["status"] != "WAITING":
        return client.mutate("POST", route + "/complete", {})
    while True:
        while client.read(route)["waiting_for_executions"]:
            time.sleep(1)
        try:
            started = client.mutate("POST", route + "/start", {})["operation_result"]
            break
        except Problem as exc:
            if exc.code != "maintenance_waiting":
                raise
            time.sleep(1)

    def persist(value):
        client.mutate(
            "POST", route + "/process", {k: value[k] for k in ("pid", "creation_time", "assigned")}
        )

    host = JobHost(started["execution"], persist, command=started["command"], terminal_stderr=True)
    try:
        host.send(started["engine_request"])
        try:
            while not host.exited() or host.active_count():
                try:
                    line = host.messages.get(timeout=0.1)
                except queue.Empty:
                    continue
                if line:
                    message = json.loads(line)
                    if message.get("type") == "account_identified":
                        while True:
                            identified = client.mutate("POST", route + "/identified", {})[
                                "operation_result"
                            ]
                            if identified["ready"]:
                                break
                            while client.read(route)["waiting_for_executions"]:
                                time.sleep(1)
                        host.send(
                            {
                                "type": "maintenance_ready",
                                "operation_id": operation_id,
                                "verified_mid": identified["verified_mid"],
                            }
                        )
        except KeyboardInterrupt:
            if not host.stop():
                raise Problem(
                    "maintenance_exit_unconfirmed", "登录已取消，但维护进程尚未确认退出", 409
                )
        # Keep the Job handle alive until Core has independently recorded whole-tree exit.
        while True:
            result = client.mutate("POST", route + "/complete", {})
            if result["operation_result"]["status"] != "VERIFYING":
                return result
            time.sleep(1)
    finally:
        host.close()
