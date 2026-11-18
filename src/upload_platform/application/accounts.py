"""Account maintenance is independent of upload Tasks and never trusts CLI success."""

import json
import os
from pathlib import Path
import threading
import time
from typing import Literal
from uuid import UUID

from pydantic import Field
from sqlalchemy import delete, insert, select, update

from upload_platform.application.contracts import StrictModel, Empty
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import JobHost, boot_id, stop_verified


class MaintenanceRequest(StrictModel):
    kind: Literal["login", "validate", "renew"] = "login"
    account_id: str | None = None
    import_file: str | None = None


class ProcessIdentity(StrictModel):
    pid: int = Field(gt=0)
    creation_time: str
    assigned: Literal[True]


class AccountEnabled(StrictModel):
    enabled: bool


ACTIVE = ["WAITING", "READY", "STARTING", "RUNNING", "VERIFYING", "UNCERTAIN"]


def schedule_renewal(conn, account_id, execution, at):
    account = db.get(conn, db.accounts, account_id)
    reference = execution.get("credential_reference")
    if reference != account["credential_reference"]:
        return
    if db.one(
        conn,
        select(db.account_blocks).where(
            db.account_blocks.c.account_id == account_id,
            db.account_blocks.c.reason == "credential_invalid",
            db.account_blocks.c.owner == reference,
        ),
    ):
        return
    conn.execute(
        insert(db.account_blocks).values(
            id=uid(),
            account_id=account_id,
            reason="credential_invalid",
            owner=reference,
            created_at=at,
        )
    )
    if db.one(
        conn,
        select(db.account_maintenance).where(
            db.account_maintenance.c.account_id == account_id,
            db.account_maintenance.c.status.in_(ACTIVE),
        ),
    ):
        return
    identifier = uid()
    conn.execute(
        insert(db.account_maintenance).values(
            id=identifier,
            account_id=account_id,
            kind="renew",
            status="WAITING",
            generation=1,
            expected_mid=account["remote_user_id"],
            old_credential_reference=reference,
            execution={"engine_build_id": execution["engine_build_id"], "automatic": True},
            created_at=at,
            updated_at=at,
        )
    )
    conn.execute(
        insert(db.account_blocks).values(
            id=uid(), account_id=account_id, reason="maintenance", owner=identifier, created_at=at
        )
    )


class Accounts:
    def __init__(self, service):
        self.service = service
        self.database = service.database
        self.lock = threading.RLock()
        self.observers = {}

    def observe(self, operation_id):
        """Retain independent Job handles across terminal loss, without killing uploads."""
        import win32api
        import win32event
        import win32job
        from upload_platform.infrastructure.windows import process_creation

        with self.lock:
            if operation_id in self.observers:
                return
            operation = self.get(operation_id)
            execution = operation["execution"]
            if execution.get("exit_confirmed") or not execution.get("assigned"):
                return
            if execution.get("boot_id") != boot_id():
                return
            process = win32api.OpenProcess(0x00100000 | 0x1000, False, execution["pid"])
            try:
                if process_creation(process) != execution["creation_time"]:
                    raise Problem("process_identity_mismatch", "维护进程身份不符", 409)
                job = win32job.OpenJobObject(0x0004, False, execution["job_name"])
            except BaseException:
                process.Close()
                raise
            self.observers[operation_id] = (process, job)

        def watch():
            try:
                while (
                    win32event.WaitForSingleObject(process, 0) != win32event.WAIT_OBJECT_0
                    or win32job.QueryInformationJobObject(
                        job, win32job.JobObjectBasicAccountingInformation
                    )["ActiveProcesses"]
                ):
                    time.sleep(0.1)
                with self.database.transaction() as conn:
                    current = db.get(conn, db.account_maintenance, operation_id)
                    if current["execution"].get("creation_time") == execution["creation_time"]:
                        conn.execute(
                            update(db.account_maintenance)
                            .where(db.account_maintenance.c.id == operation_id)
                            .values(execution={**current["execution"], "exit_confirmed": True})
                        )
            finally:
                process.Close()
                job.Close()
                with self.lock:
                    self.observers.pop(operation_id, None)

        threading.Thread(target=watch, daemon=True).start()

    def recover_observers(self):
        import pywintypes

        with self.database.read() as conn:
            operations = db.many(
                conn,
                select(db.account_maintenance).where(db.account_maintenance.c.status.in_(ACTIVE)),
            )
        for operation in operations:
            try:
                self.observe(operation["id"])
            except (OSError, pywintypes.error, Problem):
                pass  # Keep the persisted pause; an unreadable identity is never exit proof.

    def deployment(self):
        engine = self.database.config.bilibili
        if engine is None:
            raise Problem("engine_not_configured", "尚未配置 Bilibili 引擎", 409)
        engine.capabilities()
        return engine

    def run_automatic_once(self):
        import pywintypes

        with self.database.read() as conn:
            operations = db.many(
                conn,
                select(db.account_maintenance).where(
                    db.account_maintenance.c.kind.in_(["renew", "validate"]),
                    db.account_maintenance.c.status.in_(ACTIVE),
                ),
            )
        for operation in operations:
            if not operation["execution"].get("automatic"):
                continue
            if operation["status"] != "WAITING":
                owner = operation["execution"].get("coordinator_pid")
                if (
                    owner
                    and owner != os.getpid()
                    and operation["execution"].get("boot_id") == boot_id()
                ):
                    import win32api
                    import win32event
                    from upload_platform.infrastructure.windows import process_creation

                    try:
                        handle = win32api.OpenProcess(0x00100000 | 0x1000, False, owner)
                    except (OSError, pywintypes.error) as exc:
                        if getattr(exc, "winerror", None) != 87:
                            continue
                    else:
                        try:
                            if (
                                process_creation(handle)
                                == operation["execution"].get("coordinator_creation_time")
                                and win32event.WaitForSingleObject(handle, 0)
                                != win32event.WAIT_OBJECT_0
                            ):
                                continue
                        finally:
                            handle.Close()
                try:
                    self.observe(operation["id"])
                    self.complete(operation["id"], uid())
                except (OSError, Problem):
                    continue
                return
            try:
                started = self.start(operation["id"], uid())[1]
            except Problem as exc:
                if exc.code in ("maintenance_waiting", "maintenance_already_started"):
                    continue
                raise

            def persist(value):
                self.process(
                    operation["id"],
                    uid(),
                    ProcessIdentity(**{k: value[k] for k in ("pid", "creation_time", "assigned")}),
                )

            host = JobHost(started["execution"], persist, command=started["command"])
            try:
                host.send(started["engine_request"])
                deadline = time.monotonic() + 150
                while (not host.exited() or host.active_count()) and time.monotonic() < deadline:
                    time.sleep(0.1)
                if not host.exited() or host.active_count():
                    if not host.stop():
                        return
                self.complete(operation["id"], uid())
            finally:
                host.close()
            return

    def startup_validate(self):
        if self.database.config.bilibili is None:
            return
        with self.database.transaction() as conn:
            for account in db.many(
                conn, select(db.accounts).where(db.accounts.c.platform == "bilibili")
            ):
                if db.one(
                    conn,
                    select(db.account_maintenance).where(
                        db.account_maintenance.c.account_id == account["id"],
                        db.account_maintenance.c.status.in_(ACTIVE),
                    ),
                ):
                    continue
                identifier, at = uid(), self.service.clock()
                conn.execute(
                    insert(db.account_maintenance).values(
                        id=identifier,
                        account_id=account["id"],
                        kind="validate",
                        status="WAITING",
                        generation=1,
                        expected_mid=account["remote_user_id"],
                        old_credential_reference=account["credential_reference"],
                        execution={
                            "engine_build_id": self.database.config.bilibili.build_id,
                            "automatic": True,
                            "reason": "worker_startup",
                        },
                        created_at=at,
                        updated_at=at,
                    )
                )
                conn.execute(
                    insert(db.account_blocks).values(
                        id=uid(),
                        account_id=account["id"],
                        reason="maintenance",
                        owner=identifier,
                        created_at=at,
                    )
                )

    def get(self, operation_id):
        with self.database.read() as conn:
            operation = db.get(conn, db.account_maintenance, operation_id)
            account_id = operation["account_id"]
            waiting = False
            if account_id:
                waiting = (
                    db.one(
                        conn,
                        select(db.leases)
                        .join(db.attempts, db.leases.c.attempt_id == db.attempts.c.id)
                        .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                        .where(
                            db.tasks.c.account_id == account_id, db.leases.c.released_at.is_(None)
                        ),
                    )
                    is not None
                )
                waiting = (
                    waiting
                    or db.one(
                        conn,
                        select(db.remote_queries).where(
                            db.remote_queries.c.account_id == account_id,
                            db.remote_queries.c.status != "FINISHED",
                        ),
                    )
                    is not None
                )
            return {**operation, "waiting_for_executions": waiting}

    def create(self, key, body, *, login_session=False):
        engine = self.deployment()
        if body.import_file is not None and (
            body.kind != "login" or not Path(body.import_file).is_absolute()
        ):
            raise Problem("invalid_import_path", "仅登录可导入本机绝对路径的凭据文件")

        def action(conn, _):
            account = db.get(conn, db.accounts, body.account_id) if body.account_id else None
            if account and account["platform"] != "bilibili":
                raise Problem("invalid_account", "该操作仅用于 Bilibili 账号")
            if not account and body.kind != "login":
                raise Problem("account_required", "验证和续期需要既有账号")
            if not account and db.one(
                conn,
                select(db.account_maintenance).where(
                    db.account_maintenance.c.kind == "login",
                    db.account_maintenance.c.expected_mid.is_(None),
                    db.account_maintenance.c.status.in_(ACTIVE),
                ),
            ):
                raise Problem("maintenance_active", "已有待识别账号的登录，请继续原操作", 409)
            if account and db.one(
                conn,
                select(db.account_maintenance).where(
                    db.account_maintenance.c.account_id == account["id"],
                    db.account_maintenance.c.status.in_(ACTIVE),
                ),
            ):
                raise Problem("maintenance_active", "该账号已有维护操作，请继续原操作", 409)
            identifier, at = uid(), self.service.clock()
            pending = (
                []
                if not account
                else [
                    x["id"]
                    for x in db.many(
                        conn,
                        select(db.attempts)
                        .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                        .where(
                            db.tasks.c.account_id == account["id"],
                            db.attempts.c.exit_confirmed.is_(False),
                            db.attempts.c.ended_at.is_(None),
                        ),
                    )
                ]
            )
            record = dict(
                id=identifier,
                account_id=body.account_id,
                kind=body.kind,
                status="WAITING",
                generation=1,
                expected_mid=account["remote_user_id"] if account else None,
                old_credential_reference=account["credential_reference"] if account else None,
                execution={
                    "engine_build_id": engine.build_id,
                    "waiting_attempts": pending,
                    "import_file": body.import_file,
                },
                created_at=at,
                updated_at=at,
            )
            if login_session:
                record["execution"]["login_session"] = {
                    "version": 1,
                    "revision": 1,
                    "phase": "queued",
                    "outcome": None,
                    "cancel_requested": False,
                    "error": None,
                }
            conn.execute(insert(db.account_maintenance).values(**record))
            if account:
                conn.execute(
                    insert(db.account_blocks).values(
                        id=uid(),
                        account_id=account["id"],
                        reason="maintenance",
                        owner=identifier,
                        created_at=at,
                    )
                )
            return (
                {"session_id": identifier, "operation_id": identifier} if login_session else record
            )

        return self.service.mutate(
            "account-login-sessions:create" if login_session else "account-maintenance:create",
            key,
            body,
            lambda: None,
            action,
            202 if login_session else 201,
        )

    def start(self, operation_id, key):
        engine = self.deployment()

        def action(conn, _):
            operation = db.get(conn, db.account_maintenance, operation_id)
            if operation["execution"].get("login_session", {}).get("cancel_requested"):
                raise Problem("login_cancelled", "登录取消已受理", 409)
            if operation["status"] != "WAITING":
                raise Problem("maintenance_already_started", "维护已启动；只能继续核验原操作", 409)
            if operation["execution"]["engine_build_id"] != engine.build_id:
                raise Problem("engine_incompatible", "维护操作固定的引擎版本已不可用", 409)
            if operation["account_id"]:
                if db.one(
                    conn,
                    select(db.remote_queries).where(
                        db.remote_queries.c.account_id == operation["account_id"],
                        db.remote_queries.c.status != "FINISHED",
                    ),
                ):
                    raise Problem("maintenance_waiting", "正在等待该账号的查询进程退出", 409)
                leases = db.one(
                    conn,
                    select(db.leases)
                    .join(db.attempts, db.leases.c.attempt_id == db.attempts.c.id)
                    .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                    .where(
                        db.tasks.c.account_id == operation["account_id"],
                        db.leases.c.released_at.is_(None),
                    ),
                )
                if leases:
                    raise Problem(
                        "maintenance_waiting", "正在等待该账号的已领取任务及进程退出", 409
                    )
            directory = self.database.config.data_dir / "maintenance" / operation_id
            directory.mkdir(parents=True, exist_ok=True)
            execution = {
                **operation["execution"],
                "boot_id": boot_id(),
                "job_name": "Local\\UploadPlatform-Maintenance-" + operation_id,
                "result_path": str(directory / "result.json"),
                "stderr_path": str(directory / "stderr.log"),
            }
            import win32api
            from upload_platform.infrastructure.windows import process_creation

            execution.update(
                coordinator_pid=os.getpid(),
                coordinator_creation_time=process_creation(win32api.GetCurrentProcess()),
            )
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == operation_id)
                .values(status="STARTING", execution=execution, updated_at=self.service.clock())
            )
            request = dict(
                protocol_version=1,
                operation_id=operation_id,
                state_root=str(engine.state_root),
                result_path=execution["result_path"],
                expected_mid=operation["expected_mid"],
                credential_reference=operation["old_credential_reference"],
                import_file=operation["execution"].get("import_file"),
                coordinate_registration=operation["account_id"] is None,
            )
            return {
                "id": operation_id,
                "execution": execution,
                "command": [*engine.command, operation["kind"]],
                "engine_request": request,
            }

        return self.service.mutate(
            "account-maintenance:start:" + operation_id, key, Empty(), lambda: None, action
        )

    def process(self, operation_id, key, body):
        def action(conn, _):
            operation = db.get(conn, db.account_maintenance, operation_id)
            if operation["status"] != "STARTING":
                raise Problem("maintenance_fenced", "维护进程身份已固定", 409)
            execution = {**operation["execution"], **body.model_dump()}
            # Verify ownership while the CLI still holds the suspended process and Job.
            import win32api
            import win32job
            from upload_platform.infrastructure.windows import process_creation

            process = win32api.OpenProcess(0x1000, False, body.pid)
            job = win32job.OpenJobObject(0x0004, False, execution["job_name"])
            try:
                if process_creation(process) != body.creation_time or not win32job.IsProcessInJob(
                    process, job
                ):
                    raise Problem("process_identity_mismatch", "维护进程身份不符", 409)
            finally:
                process.Close()
                job.Close()
            conn.execute(
                update(db.account_maintenance)
                .where(db.account_maintenance.c.id == operation_id)
                .values(status="RUNNING", execution=execution, updated_at=self.service.clock())
            )
            return {"id": operation_id, "status": "RUNNING"}

        result = self.service.mutate(
            "account-maintenance:process:" + operation_id, key, body, lambda: None, action
        )
        self.observe(operation_id)
        return result

    def identified(self, operation_id, key):
        operation = self.get(operation_id)
        path = Path(operation["execution"]["result_path"]).with_name("identified.json")
        try:
            if path.resolve() != path.absolute() or path.stat().st_size > 4096:
                raise ValueError()
            evidence = json.loads(path.read_text(encoding="utf-8"))
            mid = evidence["verified_mid"]
            if (
                evidence
                != {
                    "protocol_version": 1,
                    "engine_build_id": operation["execution"]["engine_build_id"],
                    "operation_id": operation_id,
                    "type": "account_identified",
                    "verified_mid": mid,
                }
                or type(evidence["protocol_version"]) is not int
                or not isinstance(mid, str)
                or not mid.isascii()
                or not mid.isdecimal()
                or int(mid) <= 0
                or str(int(mid)) != mid
            ):
                raise ValueError()
        except (OSError, ValueError, KeyError) as exc:
            raise Problem(
                "maintenance_identity_unverified", "未找到可靠的账号身份核验记录", 409
            ) from exc

        def action(conn, _):
            current = db.get(conn, db.account_maintenance, operation_id)
            if current["status"] != "RUNNING" or (
                current["expected_mid"] is not None and current["expected_mid"] != mid
            ):
                raise Problem("maintenance_fenced", "维护操作阶段或账号身份不符", 409)
            account = db.one(
                conn,
                select(db.accounts).where(
                    db.accounts.c.platform == "bilibili", db.accounts.c.remote_user_id == mid
                ),
            )
            ready = True
            if account:
                if db.one(
                    conn,
                    select(db.account_maintenance).where(
                        db.account_maintenance.c.account_id == account["id"],
                        db.account_maintenance.c.id != operation_id,
                        db.account_maintenance.c.status.in_(ACTIVE),
                    ),
                ):
                    raise Problem("maintenance_active", "同 MID 已有其他维护操作", 409)
                if current["account_id"] is None:
                    conn.execute(
                        insert(db.account_blocks).values(
                            id=uid(),
                            account_id=account["id"],
                            reason="maintenance",
                            owner=operation_id,
                            created_at=self.service.clock(),
                        )
                    )
                active = db.one(
                    conn,
                    select(db.leases)
                    .join(db.attempts, db.leases.c.attempt_id == db.attempts.c.id)
                    .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                    .where(
                        db.tasks.c.account_id == account["id"], db.leases.c.released_at.is_(None)
                    ),
                )
                query = db.one(
                    conn,
                    select(db.remote_queries).where(
                        db.remote_queries.c.account_id == account["id"],
                        db.remote_queries.c.status != "FINISHED",
                    ),
                )
                ready = active is None and query is None
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == operation_id)
                    .values(
                        account_id=account["id"],
                        expected_mid=mid,
                        old_credential_reference=account["credential_reference"],
                    )
                )
            else:
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == operation_id)
                    .values(expected_mid=mid)
                )
            return {"id": operation_id, "verified_mid": mid, "ready": ready}

        return self.service.mutate(
            "account-maintenance:identified:" + operation_id, key, Empty(), lambda: None, action
        )

    def _evidence(self, operation):
        path = Path(operation["execution"]["result_path"])
        if path.resolve() != path.absolute() or path.stat().st_size > 65536:
            raise ValueError("unsafe result")
        result = json.loads(path.read_text(encoding="utf-8"))
        if (
            type(result.get("protocol_version")) is not int
            or result["protocol_version"] != 1
            or result.get("engine_build_id") != operation["execution"]["engine_build_id"]
            or result.get("operation_id") != operation["id"]
        ):
            raise ValueError("wrong maintenance identity")
        if result.get("outcome") == "succeeded":
            mid = result.get("verified_mid", "")
            if not mid.isascii() or not mid.isdecimal() or str(int(mid)) != mid or int(mid) <= 0:
                raise ValueError("wrong MID")
            reference = result.get("credential_reference", "")
            prefix = mid + "-"
            if not reference.startswith(prefix) or not reference.endswith(".json"):
                raise ValueError("invalid reference")
            if str(UUID(reference[len(prefix) : -5])) != reference[len(prefix) : -5]:
                raise ValueError("invalid reference")
            if (
                operation.get("kind") in ("login", "renew")
                and reference != f"{mid}-{operation['id']}.json"
            ):
                raise ValueError("credential version is not owned by this operation")
            if operation["expected_mid"] and mid != operation["expected_mid"]:
                raise ValueError("wrong MID")
            return {
                k: result[k]
                for k in ("outcome", "verified_mid", "display_name", "credential_reference")
            }
        code = result.get("error", {})
        code = code.get("code") if isinstance(code, dict) else None
        allowed = {
            "QR_REQUEST_FAILED",
            "QR_UNAVAILABLE",
            "LOGIN_PLATFORM_ERROR",
            "LOGIN_NETWORK_FAILED",
            "LOGIN_PROTOCOL_ERROR",
            "LOGIN_LOCAL_TIMEOUT",
            "COORDINATOR_UNAVAILABLE",
            "MID_MISMATCH",
            "IDENTITY_QUERY_FAILED",
            "CREDENTIAL_INVALID",
        }
        return {
            "outcome": "failed",
            "error": code if code in allowed else "engine_maintenance_failed",
        }

    def _validate(self, operation, mid, reference):
        """Independent read-only engine invocation. Never accepts CLI assertions as proof."""
        engine = self.deployment()
        if engine.build_id != operation["execution"]["engine_build_id"]:
            raise Problem("engine_incompatible", "原维护引擎版本不可用", 409)
        identifier = uid()
        directory = self.database.config.data_dir / "maintenance" / operation["id"] / identifier
        directory.mkdir()
        execution = {
            "boot_id": boot_id(),
            "job_name": "Local\\UploadPlatform-Validate-" + identifier,
            "result_path": str(directory / "result.json"),
            "stderr_path": str(directory / "stderr.log"),
            "engine_build_id": engine.build_id,
        }

        def persist(value):
            with self.database.transaction() as conn:
                current = db.get(conn, db.account_maintenance, operation["id"])
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == operation["id"])
                    .values(execution={**current["execution"], "validation_execution": value})
                )

        # A previous validation process must be gone before another uses the credential.
        previous = operation["execution"].get("validation_execution")
        if previous and not previous.get("exit_confirmed"):
            proof, _ = stop_verified(previous)
            if not proof:
                raise Problem("maintenance_exit_unconfirmed", "前次凭据验证进程树尚未确认退出", 409)
        persist(execution)
        host = JobHost(execution, persist, command=[*engine.command, "validate"])
        try:
            host.send(
                dict(
                    protocol_version=1,
                    operation_id=identifier,
                    state_root=str(engine.state_root),
                    result_path=execution["result_path"],
                    expected_mid=mid,
                    credential_reference=reference,
                    import_file=None,
                )
            )
            deadline = time.monotonic() + 75
            while not host.exited() and time.monotonic() < deadline:
                time.sleep(0.05)
            if host.active_count() or not host.exited():
                if not host.stop():
                    raise Problem("maintenance_exit_unconfirmed", "凭据验证进程尚未退出", 409)
            persist({**host.execution, "exit_confirmed": True})
            checked = self._evidence(
                {"id": identifier, "expected_mid": mid, "execution": execution}
            )
            return (
                checked["outcome"] == "succeeded" and checked["credential_reference"] == reference
            )
        except (OSError, ValueError, KeyError):
            return False
        finally:
            host.close()

    def complete(self, operation_id, key):
        with self.lock:
            operation = self.get(operation_id)
            if operation["status"] in ("SUCCEEDED", "FAILED"):
                return 200, operation
            execution = operation["execution"]
            if not execution.get("exit_confirmed"):
                proof, reason = stop_verified(execution, timeout=0.1, terminate=False)
                if not proof:
                    raise Problem(
                        "maintenance_exit_unconfirmed",
                        "维护进程树尚未确认退出",
                        409,
                        {"reason": reason},
                    )
            # The observer may have recorded exit first. Enter VERIFYING either way,
            # and merge the latest metadata rather than overwriting a concurrent intent.
            with self.database.transaction() as conn:
                operation = db.get(conn, db.account_maintenance, operation_id)
                if operation["status"] in ("SUCCEEDED", "FAILED"):
                    return 200, operation
                execution = {**operation["execution"], "exit_confirmed": True}
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == operation_id)
                    .values(execution=execution, status="VERIFYING")
                )
            operation["execution"] = execution
            if operation["account_id"] and not execution.get("never_started"):
                with self.database.read() as conn:
                    active = db.one(
                        conn,
                        select(db.leases)
                        .join(db.attempts, db.leases.c.attempt_id == db.attempts.c.id)
                        .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                        .where(
                            db.tasks.c.account_id == operation["account_id"],
                            db.leases.c.released_at.is_(None),
                        ),
                    )
                if active:
                    raise Problem("maintenance_waiting", "正在等待原账号执行退出", 409)
            try:
                result = self._evidence(operation)
            except (OSError, ValueError, KeyError):
                result = {"outcome": "failed", "error": "maintenance_result_missing_or_invalid"}
            succeeded = result["outcome"] == "succeeded"
            mid = result["verified_mid"] if succeeded else operation["expected_mid"]
            reference = (
                result["credential_reference"]
                if succeeded
                else operation["old_credential_reference"]
            )
            verified = bool(mid and reference and self._validate(operation, mid, reference))

            def action(conn, _):
                current = db.get(conn, db.account_maintenance, operation_id)
                if current["status"] in ("SUCCEEDED", "FAILED"):
                    return current
                account_id = current["account_id"]
                if succeeded and verified:
                    existing = db.one(
                        conn,
                        select(db.accounts).where(
                            db.accounts.c.platform == "bilibili",
                            db.accounts.c.remote_user_id == mid,
                        ),
                    )
                    if existing and account_id is None:
                        # Repeated registration enters the existing account's maintenance domain.
                        account_id = existing["id"]
                        if db.one(
                            conn,
                            select(db.account_maintenance).where(
                                db.account_maintenance.c.account_id == account_id,
                                db.account_maintenance.c.status.in_(ACTIVE),
                            ),
                        ):
                            raise Problem(
                                "maintenance_active",
                                "同 MID 已有维护；保留本次凭据版本等待核验",
                                409,
                            )
                        conn.execute(
                            insert(db.account_blocks).values(
                                id=uid(),
                                account_id=account_id,
                                reason="maintenance",
                                owner=operation_id,
                                created_at=self.service.clock(),
                            )
                        )
                        active = db.one(
                            conn,
                            select(db.leases)
                            .join(db.attempts, db.leases.c.attempt_id == db.attempts.c.id)
                            .join(db.tasks, db.attempts.c.task_id == db.tasks.c.id)
                            .where(
                                db.tasks.c.account_id == account_id,
                                db.leases.c.released_at.is_(None),
                            ),
                        )
                        if active:
                            conn.execute(
                                update(db.account_maintenance)
                                .where(db.account_maintenance.c.id == operation_id)
                                .values(
                                    account_id=account_id,
                                    expected_mid=mid,
                                    old_credential_reference=existing["credential_reference"],
                                    status="VERIFYING",
                                )
                            )
                            return {
                                "id": operation_id,
                                "status": "VERIFYING",
                                "account_id": account_id,
                            }
                    if account_id:
                        conn.execute(
                            update(db.accounts)
                            .where(db.accounts.c.id == account_id)
                            .values(
                                credential_reference=reference,
                                display_name=result["display_name"],
                                version=db.accounts.c.version + 1,
                            )
                        )
                    else:
                        account_id = uid()
                        conn.execute(
                            insert(db.accounts).values(
                                id=account_id,
                                platform="bilibili",
                                reference="bilibili:" + mid,
                                display_name=result["display_name"],
                                status="ACTIVE",
                                concurrency_limit=3,
                                remote_user_id=mid,
                                credential_reference=reference,
                            )
                        )
                status = "SUCCEEDED" if succeeded and verified else "FAILED"
                if account_id:
                    conn.execute(
                        delete(db.account_blocks).where(
                            db.account_blocks.c.account_id == account_id,
                            db.account_blocks.c.owner == operation_id,
                            db.account_blocks.c.reason == "maintenance",
                        )
                    )
                    old_reference = current["old_credential_reference"]
                    if verified:
                        conn.execute(
                            delete(db.account_blocks).where(
                                db.account_blocks.c.account_id == account_id,
                                db.account_blocks.c.owner == old_reference,
                                db.account_blocks.c.reason.in_(
                                    ["credential_unverified", "credential_invalid"]
                                ),
                            )
                        )
                    elif not db.one(
                        conn,
                        select(db.account_blocks).where(
                            db.account_blocks.c.account_id == account_id,
                            db.account_blocks.c.owner == old_reference,
                            db.account_blocks.c.reason == "credential_unverified",
                        ),
                    ):
                        conn.execute(
                            insert(db.account_blocks).values(
                                id=uid(),
                                account_id=account_id,
                                owner=old_reference,
                                reason="credential_unverified",
                                created_at=self.service.clock(),
                            )
                        )
                changes = dict(
                    account_id=account_id,
                    status=status,
                    result=result,
                    credential_reference=reference if succeeded and verified else None,
                    error=None if succeeded and verified else "maintenance_not_verified",
                    updated_at=self.service.clock(),
                )
                conn.execute(
                    update(db.account_maintenance)
                    .where(db.account_maintenance.c.id == operation_id)
                    .values(**changes)
                )
                return {**current, **changes}

            return self.service.mutate(
                "account-maintenance:complete:" + operation_id, key, Empty(), lambda: None, action
            )

    def enabled(self, account_id, key, body):
        def action(conn, _):
            db.get(conn, db.accounts, account_id)
            conn.execute(
                update(db.accounts)
                .where(db.accounts.c.id == account_id)
                .values(enabled=body.enabled, version=db.accounts.c.version + 1)
            )
            return db.get(conn, db.accounts, account_id)

        return self.service.mutate("account:enabled:" + account_id, key, body, lambda: None, action)
