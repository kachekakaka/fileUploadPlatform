from concurrent.futures import ThreadPoolExecutor
import logging
from pathlib import Path
import queue
import threading
import time

from pydantic import ValidationError

from upload_platform.application.service import Service
from upload_platform.domain.core import Problem
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.protocol import (
    identity,
    HostRequest,
    HostEvent,
    HostResult,
    verify_identity,
)
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import asset_info, write_json
from upload_platform.infrastructure.windows import JobHost, boot_id, stop_verified
from upload_platform.scheduling.runtime import Scheduler

logger = logging.getLogger(__name__)


def read_result(attempt):
    path = attempt["execution"].get("result_path")
    if not path or not Path(path).exists():
        return None
    try:
        if attempt["execution"].get("adapter") == "bilibili":
            import json
            from upload_platform.adapters.bilibili.protocol import verify_result

            result_file = Path(path)
            if str(result_file.resolve()).casefold() != str(result_file.absolute()).casefold():
                raise Problem("invalid_result_path", "结果路径不得重定向", 409)
            with result_file.open("rb") as source:
                content = source.read(4 * 1024 * 1024 + 1)
            if len(content) > 4 * 1024 * 1024:
                raise Problem("invalid_result", "引擎结果超过大小限制", 409)
            return verify_result(json.loads(content), attempt).model_dump()
        result = HostResult.model_validate_json(Path(path).read_text(encoding="utf-8"))
        verify_identity(result, attempt)
        if attempt["permission_at"] is None or result.host_sequence < attempt["host_sequence"]:
            raise Problem("invalid_result", "结果阶段或序号无效", 409)
        return result.model_dump()
    except (ValueError, ValidationError, OSError) as exc:
        raise Problem("invalid_result", "结果文件结构无效", 409) from exc


class Supervisor:
    def __init__(self, store, fault=None):
        self.store, self.config = store, store.config
        self.fault = fault or (lambda point: None)

    def current(self, original):
        with self.store.database.read() as conn:
            return db.get(conn, db.attempts, original["id"]), db.get(
                conn, db.tasks, original["task_id"]
            )

    def execute(self, original):
        host = None
        try:
            attempt, task = self.current(original)
            if task["snapshot"]["target"]["platform"] == "bilibili":
                from upload_platform.adapters.bilibili.runtime import execute

                execute(self, original)
                return
            if task["cancel_requested_at"]:
                self.store.finish(original, "cancelled", proof="no_execution_intent")
                return
            try:
                for asset in task["snapshot"]["assets"]:
                    asset_info(task["snapshot"]["directory"], asset["relative_path"])
            except Problem:
                self.store.finish(
                    original, "permanent", proof="no_execution_intent", error="asset_unavailable"
                )
                return
            directory = self.config.data_dir / "executions" / original["id"]
            directory.mkdir(parents=True, exist_ok=False)
            execution = {
                "boot_id": boot_id(),
                "job_name": "Local\\UploadPlatform-" + original["execution_id"],
                "result_path": str(directory / "result.json"),
                "evidence_path": str(directory / "observation.json"),
                "stderr_path": str(directory / "stderr.log"),
                "adapter": "fake",
                "adapter_version": "1",
                "protocol_version": 1,
            }
            if not self.store.starting(original, execution):
                self.store.finish(original, "cancelled", proof="no_execution_intent")
                return
            self.fault("after_intent")
            host = JobHost(
                execution, lambda data: self.store.process_identity(original, data), self.fault
            )
            request = HostRequest(
                **identity(original),
                attempt_number=original["attempt_number"],
                options=task["snapshot"]["target"]["options"],
                result_path=execution["result_path"],
                evidence_path=execution["evidence_path"],
            )
            host.send(request.model_dump())
            started, heartbeat = time.monotonic(), time.monotonic()
            granted = False
            cancelled_at = None
            error = None
            while True:
                elapsed = time.monotonic() - started
                if time.monotonic() - heartbeat >= self.config.heartbeat_seconds:
                    self.store.heartbeat(original)
                    heartbeat = time.monotonic()
                attempt, task = self.current(original)
                if task["cancel_requested_at"] and cancelled_at is None:
                    host.send({**identity(original), "type": "cancel"})
                    cancelled_at = time.monotonic()
                if (
                    (not granted and elapsed > self.config.handshake_timeout)
                    or elapsed > self.config.execution_timeout
                    or (
                        cancelled_at
                        and time.monotonic() - cancelled_at > self.config.cancel_timeout
                    )
                ):
                    error = "execution_timeout"
                    break
                try:
                    line = host.messages.get(timeout=min(self.config.poll_seconds, 0.1))
                except queue.Empty:
                    if host.exited() and host.messages.empty():
                        break
                    continue
                if line is None:
                    break
                try:
                    message = HostEvent.model_validate_json(line)
                    verify_identity(message, original)
                    accepted = self.store.host_event(original, message)
                    if accepted and message.type == "hello":
                        if self.store.permit(original):
                            self.fault("after_permission")
                            host.send({**identity(original), "type": "go"})
                            granted = True
                        else:
                            host.send({**identity(original), "type": "cancel"})
                    if message.type == "result" and message.result_path != execution["result_path"]:
                        raise Problem("invalid_result_path", "结果引用不属于当前执行", 409)
                except (ValidationError, Problem) as exc:
                    if isinstance(exc, Problem) and exc.code == "execution_fenced":
                        raise
                    error = "invalid_host_message"
                    write_json(
                        directory / "protocol-error.json",
                        {"code": error, "attempt_id": original["id"]},
                    )
                    break
            # Host may emit a valid result before exiting; wait boundedly for its entire job.
            deadline = time.monotonic() + self.config.cancel_timeout
            while time.monotonic() < deadline and not host.exited():
                if time.monotonic() - heartbeat >= self.config.heartbeat_seconds:
                    self.store.heartbeat(original)
                    heartbeat = time.monotonic()
                time.sleep(0.02)
            self.fault("before_tree_verification")
            if host.active_count() or not host.exited():
                self.store.heartbeat(original)
                exited = host.stop(self.config.cancel_timeout)
            else:
                exited = True
            if not exited:
                return  # recovery retains both occupied leases
            self.store.confirm_exit(original, "job_empty_and_host_exited")
            attempt, task = self.current(original)
            result = None
            try:
                result = read_result(attempt)
            except Problem:
                error = "invalid_result"
                write_json(
                    directory / "protocol-error.json", {"code": error, "attempt_id": original["id"]}
                )
            self.fault("after_result_before_commit")
            if result and error not in ("invalid_result", "invalid_host_message"):
                outcome = result["outcome"]
            elif attempt["permission_at"] is None:
                outcome = "cancelled" if task["cancel_requested_at"] else "retryable"
            else:
                outcome = "uncertain"
            if error in ("invalid_result", "invalid_host_message"):
                result = None
            self.store.finish(
                original, outcome, proof="job_empty_and_host_exited", result=result, error=error
            )
        except Exception as exc:
            # On ownership/database/launch failure, stop authorizing actions. Recovery alone
            # can conclude the old execution after acquiring its own generation.
            logger.warning(
                "supervision deferred: %s attempt=%s", type(exc).__name__, original["id"]
            )
        finally:
            if host:
                host.close()

    def recover(self, original):
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.config.heartbeat_seconds):
                try:
                    self.store.heartbeat(original, recovery=True)
                except Exception:
                    stop.set()

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            saved_proof = original["execution"].get("exit_proof")
            if original["exit_confirmed"] and saved_proof in (
                "job_empty_and_host_exited",
                "previous_boot_exited",
            ):
                exited, proof = True, saved_proof
            elif not original["execution"] and original["status"] == "CREATED":
                exited, proof = True, "no_execution_intent"
            else:
                exited, proof = stop_verified(original["execution"], self.config.cancel_timeout)
            if not exited:
                self.store.recovery_note(original, proof)
                return
            if proof != "no_execution_intent":
                self.store.confirm_exit(original, proof, recovery=True)
            attempt, task = self.current(original)
            error, result = None, None
            try:
                result = read_result(attempt)
            except Problem:
                error = "invalid_result"
            if result:
                outcome = result["outcome"]
            elif (
                attempt["handed_off_at"]
                if task["snapshot"]["target"]["platform"] == "bilibili"
                else attempt["permission_at"]
            ) is None:
                outcome = "cancelled" if task["cancel_requested_at"] else "retryable"
            else:
                outcome = "uncertain"
            self.store.finish(
                original, outcome, proof=proof, result=result, error=error, recovery=True
            )
            if outcome == "uncertain" and task["snapshot"]["target"]["platform"] == "bilibili":
                from upload_platform.application.remote_queries import RemoteQueries

                try:
                    RemoteQueries(Service(self.store.database)).query(
                        task["id"], original["execution_id"]
                    )
                except Problem:
                    pass  # One bounded query only; missing evidence never authorizes another upload.
        finally:
            stop.set()
            thread.join(timeout=self.config.heartbeat_seconds + 1)


class Worker:
    def __init__(self, database, *, stop=None, fault=None):
        self.database, self.config = database, database.config
        self.stop = stop or threading.Event()
        self.store = ExecutionStore(database)
        self.supervisor = Supervisor(self.store, fault)
        self.scheduler = Scheduler(
            Service(database), self.store.worker_id, self.config.lease_seconds
        )

    def run(self):
        self.database.initialize()
        self.store.register()
        from upload_platform.application.accounts import Accounts

        accounts = Accounts(Service(self.database))
        accounts.startup_validate()
        accounts.recover_observers()

        def schedule_loop():
            while not self.stop.is_set():
                try:
                    self.scheduler.tick()
                except Exception as exc:
                    logger.warning("scheduling deferred: %s", type(exc).__name__)
                self.stop.wait(self.config.poll_seconds)

        scheduler_thread = threading.Thread(target=schedule_loop, daemon=True)
        scheduler_thread.start()

        def maintenance_loop():
            while not self.stop.is_set():
                try:
                    accounts.run_automatic_once()
                except Exception as exc:
                    logger.warning("account maintenance deferred: %s", type(exc).__name__)
                self.stop.wait(max(self.config.poll_seconds, 1))

        maintenance_thread = threading.Thread(target=maintenance_loop, daemon=True)
        maintenance_thread.start()
        futures = set()
        try:
            with ThreadPoolExecutor(max_workers=16, thread_name_prefix="upload-supervisor") as pool:
                heartbeat = 0
                while not self.stop.is_set():
                    futures = {f for f in futures if not f.done()}
                    try:
                        if time.monotonic() - heartbeat >= self.config.heartbeat_seconds:
                            self.store.worker_heartbeat()
                            heartbeat = time.monotonic()
                        if len(futures) < 16:
                            recovery = self.store.recovery_claim()
                            if recovery:
                                futures.add(pool.submit(self.supervisor.recover, recovery))
                            else:
                                attempt = self.store.claim()
                                if attempt:
                                    futures.add(pool.submit(self.supervisor.execute, attempt))
                    except Exception as exc:
                        logger.warning("worker deferred: %s", type(exc).__name__)
                    self.stop.wait(self.config.poll_seconds)
        finally:
            self.stop.set()
            scheduler_thread.join(timeout=self.config.lease_seconds)
            maintenance_thread.join(timeout=self.config.lease_seconds)
            self.store.worker_heartbeat("STOPPED")
