import json
import logging
import queue
import threading
import time

from upload_platform.adapters.bilibili.mapping import upload_request
from upload_platform.adapters.bilibili.protocol import Message, identity, verify_identity
from upload_platform.domain.core import Problem
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import asset_info
from upload_platform.infrastructure.windows import JobHost, boot_id

logger = logging.getLogger(__name__)


def execute(supervisor, original):
    store, config, fault = supervisor.store, supervisor.config, supervisor.fault
    host = None
    try:
        attempt, task = supervisor.current(original)
        if task["cancel_requested_at"] is not None:
            store.finish(original, "cancelled", proof="no_execution_intent")
            return
        deployment = config.bilibili
        try:
            if (
                deployment is None
                or task["snapshot"]["target"]["engine_build_id"] != deployment.build_id
            ):
                raise Problem("engine_incompatible", "选定引擎不可用")
            for asset in task["snapshot"]["assets"]:
                asset_info(
                    task["snapshot"]["directory"],
                    asset["relative_path"],
                    fixed_root="config_version" in task["snapshot"],
                )
            with store.database.read() as conn:
                account = db.get(conn, db.accounts, task["account_id"])
            directory = config.data_dir / "executions" / original["id"]
            execution = {
                "boot_id": boot_id(),
                "job_name": "Local\\UploadPlatform-" + original["execution_id"],
                "result_path": str(directory / "result.json"),
                "stderr_path": str(directory / "stderr.log"),
                "adapter": "bilibili",
                "protocol_version": 1,
                "engine_build_id": deployment.build_id,
                "engine_executable": str(deployment.executable),
                "expected_mid": task["snapshot"]["target"]["expected_mid"],
                "credential_reference": account["credential_reference"],
            }
            request = upload_request(task, {**attempt, "execution": execution}, account, config)
        except Problem as exc:
            store.finish(original, "permanent", proof="no_execution_intent", error=exc.code)
            return
        directory.mkdir(parents=True, exist_ok=False)
        if not store.starting(original, execution):
            store.finish(original, "cancelled", proof="no_execution_intent")
            return
        fault("after_intent")
        command = [
            *deployment.command,
            "upload",
            "--identity-json",
            json.dumps(identity({**attempt, "execution": execution})),
        ]
        host = JobHost(
            execution, lambda data: store.process_identity(original, data), fault, command=command
        )
        start = heartbeat = time.monotonic()
        handed_off = False
        sent = threading.Event()
        send_failed = threading.Event()
        output_closed = False
        error = None

        def send_once():
            try:
                host.send(request)
            except (OSError, ValueError):
                send_failed.set()
            finally:
                sent.set()

        while True:
            if time.monotonic() - heartbeat >= config.heartbeat_seconds:
                store.heartbeat(original)
                heartbeat = time.monotonic()
            attempt, task = supervisor.current(original)
            if not handed_off and task["cancel_requested_at"] is not None:
                host.stdin.close()
                host.stdin = None
                break
            if not handed_off and time.monotonic() - start > config.handshake_timeout:
                error = "engine_handshake_timeout"
                break
            if send_failed.is_set():
                error = "engine_handoff_write_failed"
                break
            if getattr(host, "protocol_overflow", False):
                error = "engine_protocol_overflow"
                break
            if output_closed:
                if host.exited():
                    break
                time.sleep(min(config.poll_seconds, 0.1))
                continue
            try:
                line = host.messages.get(timeout=min(config.poll_seconds, 0.1))
            except queue.Empty:
                if host.exited() and host.messages.empty():
                    break
                continue
            if line is None:
                output_closed = True
                continue
            message = Message.model_validate_json(line)
            verify_identity(message, attempt)
            if message.type == "hello":
                if handed_off:
                    raise Problem("invalid_handshake", "交接后出现重复握手", 409)
                store.bilibili_hello(original, message)
                try:
                    allowed = store.handoff(original)
                except Problem as exc:
                    if exc.code != "b18_not_permitted":
                        raise
                    error, allowed = exc.code, False
                if not allowed:
                    host.stdin.close()
                    host.stdin = None
                    break
                handed_off = True
                fault("after_handoff")
                threading.Thread(target=send_once, daemon=True).start()
            elif message.type == "progress":
                store.bilibili_event(original, message)
            elif message.result_path != execution["result_path"]:
                raise Problem("invalid_result_path", "结果路径不属于本次执行", 409)
        deadline = time.monotonic() + config.cancel_timeout
        while not host.exited() and time.monotonic() < deadline:
            if time.monotonic() - heartbeat >= config.heartbeat_seconds:
                store.heartbeat(original)
                heartbeat = time.monotonic()
            time.sleep(0.02)
        fault("before_tree_verification")
        if host.active_count() or not host.exited():
            if not host.stop(config.cancel_timeout):
                return
        store.confirm_exit(original, "job_empty_and_host_exited")
        attempt, task = supervisor.current(original)
        from upload_platform.execution.runtime import read_result

        try:
            result = read_result(attempt)
        except Problem:
            result, error = None, "invalid_result"
        fault("after_result_before_commit")
        if result is not None:
            outcome = result["outcome"]
        elif attempt["handed_off_at"] is None:
            outcome = "cancelled" if task["cancel_requested_at"] is not None else "retryable"
        else:
            outcome = "uncertain"
        store.finish(
            original, outcome, proof="job_empty_and_host_exited", result=result, error=error
        )
        if outcome == "uncertain":
            from upload_platform.application.remote_queries import RemoteQueries
            from upload_platform.application.service import Service

            try:
                RemoteQueries(Service(store.database)).query(task["id"], original["execution_id"])
            except Problem:
                pass
    except Exception as exc:
        logger.warning(
            "Bilibili supervision deferred: %s attempt=%s", type(exc).__name__, original["id"]
        )
    finally:
        if host:
            host.close()
