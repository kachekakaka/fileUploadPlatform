import json
from pathlib import Path
import time
import threading

from sqlalchemy import insert, select, update

from upload_platform.adapters.bilibili.protocol import Message, verify_identity
from upload_platform.application.contracts import Empty
from upload_platform.application.service import task_state
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import JobHost, boot_id, stop_verified


class RemoteQueries:
    def __init__(self, service):
        self.service, self.database = service, service.database
        self.lock = threading.RLock()

    def prepare(self, task_id):
        with self.database.read() as conn:
            task = db.get(conn, db.tasks, task_id)
            if task["snapshot"]["target"]["platform"] != "bilibili" or task["status"] not in (
                "SUCCEEDED",
                "UNCERTAIN",
            ):
                raise Problem("query_not_applicable", "仅查询 Bilibili 已受理或结果不明的稿件", 409)
            attempt = db.get(conn, db.attempts, task["current_attempt_id"])
            if not attempt["exit_confirmed"]:
                raise Problem("exit_unconfirmed", "需先确认旧执行进程树退出", 409)
            receipt = db.one(conn, select(db.receipts).where(db.receipts.c.task_id == task_id))
            account = db.get(conn, db.accounts, task["account_id"])
        evidence = receipt["evidence"] if receipt and isinstance(receipt["evidence"], dict) else {}
        keys = evidence.get("video_keys")
        if not keys:
            # Read only this execution's fixed journal files; never use title similarity.
            directory = Path(attempt["execution"]["result_path"]).parent
            previous = None
            transitions = {
                None: {"request_accepted"},
                "request_accepted": {"uploading"},
                "uploading": {"waiting_submit"},
                "waiting_submit": {"submit_intent"},
            }
            for sequence in range(1, 5):
                path = directory / f"event-{sequence:06}.json"
                try:
                    if path.resolve() != path.absolute() or path.stat().st_size > 1024 * 1024:
                        raise ValueError()
                    event = Message.model_validate_json(path.read_bytes())
                    verify_identity(event, attempt)
                    if event.sequence != sequence or event.phase not in transitions.get(
                        previous, set()
                    ):
                        raise ValueError()
                    previous = event.phase
                    if event.phase == "submit_intent":
                        keys = event.details.get("video_keys")
                except (OSError, ValueError):
                    break
        if (
            not keys
            or not isinstance(keys, list)
            or any(not isinstance(x, str) or not x or len(x) > 512 for x in keys)
        ):
            raise Problem(
                "query_evidence_missing", "没有可关联本次稿件的远端视频键，请人工核验", 409
            )
        engine = self.database.config.bilibili
        if engine is None or engine.build_id != attempt["execution"]["engine_build_id"]:
            raise Problem("engine_incompatible", "本次执行的引擎版本不可用", 409)
        return task, account, engine, keys, evidence

    def query(self, task_id, key):
        with self.lock:
            return self._query(task_id, key)

    def _query(self, task_id, key):
        # Reserve a durable account operation before any credential process can be started.
        def create(conn, prepared):
            task, account, engine, keys, evidence = prepared
            if db.get(conn, db.tasks, task_id)["version"] != task["version"]:
                raise Problem("version_conflict", "任务状态或版本已经变化", 409)
            current_account = db.get(conn, db.accounts, account["id"])
            if any(
                current_account[name] != account[name]
                for name in ("remote_user_id", "credential_reference", "version")
            ):
                raise Problem("version_conflict", "查询账号已经变化", 409)
            if db.one(
                conn,
                select(db.account_maintenance).where(
                    db.account_maintenance.c.account_id == account["id"],
                    db.account_maintenance.c.status.in_(
                        ["WAITING", "READY", "STARTING", "RUNNING", "VERIFYING", "UNCERTAIN"]
                    ),
                ),
            ):
                raise Problem("maintenance_active", "账号维护期间不能启动普通查询", 409)
            identifier, at = uid(), self.service.clock()
            latest = db.one(
                conn,
                select(db.remote_queries)
                .where(db.remote_queries.c.task_id == task_id)
                .order_by(db.remote_queries.c.generation.desc())
                .limit(1),
            )
            generation = latest["generation"] + 1 if latest else 1
            directory = self.database.config.data_dir / "queries" / identifier
            execution = {
                "boot_id": boot_id(),
                "job_name": "Local\\UploadPlatform-Query-" + identifier,
                "result_path": str(directory / "result.json"),
                "stderr_path": str(directory / "stderr.log"),
                "engine_build_id": engine.build_id,
                "task_version": task["version"],
                "request": {
                    "protocol_version": 1,
                    "operation_id": identifier,
                    "state_root": str(engine.state_root),
                    "result_path": str(directory / "result.json"),
                    "expected_mid": account["remote_user_id"],
                    "credential_reference": account["credential_reference"],
                    "aid": evidence.get("aid"),
                    "bvid": evidence.get("bvid"),
                    "video_keys": keys,
                    "window_start": max(0, int(task["created_at"]) - 60),
                    "window_end": int(at) + 60,
                },
            }
            conn.execute(
                insert(db.remote_queries).values(
                    id=identifier,
                    account_id=account["id"],
                    task_id=task_id,
                    status="STARTING",
                    generation=generation,
                    execution=execution,
                    started_at=at,
                )
            )
            return {"query_id": identifier}

        _, reserved = self.service.mutate(
            "remote-query:" + task_id, key, Empty(), lambda: self.prepare(task_id), create, 201
        )
        with self.database.read() as conn:
            operation = db.get(conn, db.remote_queries, reserved["query_id"])
        if operation["status"] == "FINISHED":
            return 200, operation
        recovering = bool(operation["execution"].get("launch_claimed"))
        engine = self.database.config.bilibili
        execution = operation["execution"]
        if engine is None or engine.build_id != execution["engine_build_id"]:
            raise Problem("engine_incompatible", "原查询引擎版本不可用", 409)
        if recovering and not execution.get("exit_confirmed"):
            proof, _ = stop_verified(execution)
            if not proof:
                raise Problem("query_exit_unconfirmed", "原查询进程树退出未确认", 409, reserved)
            execution = {**execution, "exit_confirmed": True}
        with self.database.transaction() as conn:
            current = db.get(conn, db.remote_queries, operation["id"])
            if current["execution"].get("launch_claimed") and not recovering:
                raise Problem("query_pending", "查询已被领取", 409)
            execution = {**execution, "launch_claimed": True}
            conn.execute(
                update(db.remote_queries)
                .where(db.remote_queries.c.id == operation["id"])
                .values(execution=execution)
            )
        directory = Path(execution["result_path"]).parent
        if not recovering:
            directory.mkdir(parents=True, exist_ok=False)

        def persist(value):
            with self.database.transaction() as conn:
                conn.execute(
                    update(db.remote_queries)
                    .where(db.remote_queries.c.id == operation["id"])
                    .values(execution=value, status="RUNNING")
                )

        host = None
        result, error = None, "query_failed"
        try:
            if not recovering:
                host = JobHost(execution, persist, command=[*engine.command, "query"])
                host.send(execution["request"])
                deadline = time.monotonic() + 100
                while (not host.exited() or host.active_count()) and time.monotonic() < deadline:
                    time.sleep(0.05)
                if not host.exited() or host.active_count():
                    if not host.stop():
                        raise Problem("query_exit_unconfirmed", "查询进程树尚未退出", 409)
                execution = {**host.execution, "exit_confirmed": True}
            persist(execution)
            path = Path(execution["result_path"])
            if path.resolve() != path.absolute() or path.stat().st_size > 65536:
                raise ValueError()
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if (
                type(candidate.get("protocol_version")) is not int
                or candidate["protocol_version"] != 1
                or candidate.get("operation_id") != operation["id"]
                or candidate.get("engine_build_id") != engine.build_id
            ):
                raise ValueError()
            evidence = candidate.get("evidence")
            if candidate.get("outcome") == "matched":
                if (
                    not isinstance(evidence, dict)
                    or evidence.get("verified_mid") != execution["request"]["expected_mid"]
                    or evidence.get("video_keys") != execution["request"]["video_keys"]
                    or evidence.get("query_verified") is not True
                    or type(evidence.get("aid")) is not int
                    or evidence["aid"] <= 0
                ):
                    raise ValueError()
                for field in ("aid", "bvid"):
                    if execution["request"][field] is not None and execution["request"][
                        field
                    ] != evidence.get(field):
                        raise ValueError()
                result = {
                    name: evidence[name]
                    for name in (
                        "platform",
                        "verified_mid",
                        "aid",
                        "bvid",
                        "video_keys",
                        "query_verified",
                        "review_status",
                        "queried_at",
                    )
                }
                error = None
            else:
                error = "query_inconclusive"
        except (OSError, ValueError, KeyError):
            error = "query_invalid_or_unavailable"
        finally:
            if host:
                host.close()
        if not execution.get("exit_confirmed"):
            raise Problem("query_exit_unconfirmed", "查询退出未确认，账号维护保持等待", 409)
        with self.database.transaction() as conn:
            task = db.get(conn, db.tasks, task_id)
            receipt = db.one(conn, select(db.receipts).where(db.receipts.c.task_id == task_id))
            at = self.service.clock()
            if receipt and receipt["query_generation"] < operation["generation"]:
                values = {
                    "query_started_at": operation["started_at"],
                    "query_generation": operation["generation"],
                    "query_error": error,
                }
                if result:
                    values.update(review_status=result["review_status"], last_query_at=at)
                conn.execute(
                    update(db.receipts).where(db.receipts.c.id == receipt["id"]).values(**values)
                )
            elif (
                result
                and task["status"] == "UNCERTAIN"
                and task["version"] == execution["task_version"]
            ):
                attempt = db.get(conn, db.attempts, task["current_attempt_id"])
                if attempt["exit_confirmed"]:
                    task_state(
                        conn,
                        task,
                        "SUCCEEDED",
                        at,
                        payload={"source": "bilibili-query", "query_id": operation["id"]},
                    )
                    conn.execute(
                        insert(db.receipts).values(
                            id=uid(),
                            task_id=task_id,
                            attempt_id=attempt["id"],
                            source="bilibili-query",
                            fake=False,
                            evidence=result,
                            review_status=result["review_status"],
                            last_query_at=at,
                            query_started_at=operation["started_at"],
                            query_generation=operation["generation"],
                        )
                    )
            conn.execute(
                update(db.remote_queries)
                .where(db.remote_queries.c.id == operation["id"])
                .values(status="FINISHED", result=result, error=error, ended_at=at)
            )
            return 200, db.get(conn, db.remote_queries, operation["id"])
