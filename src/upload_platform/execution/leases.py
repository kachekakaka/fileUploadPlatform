import os

from sqlalchemy import select, insert, update

from upload_platform.application.service import task_state
from upload_platform.domain.core import Problem, uid, now, transition, ACTIVE_ATTEMPTS
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.windows import boot_id


class ExecutionStore:
    def __init__(self, database, worker_id=None, clock=now):
        self.database, self.config, self.clock = database, database.config, clock
        self.worker_id = worker_id or uid()

    def register(self):
        boot = boot_id()
        with self.database.transaction() as conn:
            conn.execute(
                insert(db.workers).values(
                    id=self.worker_id,
                    boot_id=boot,
                    pid=os.getpid(),
                    heartbeat_at=self.clock(),
                    status="RUNNING",
                )
            )

    def worker_heartbeat(self, status="RUNNING"):
        with self.database.transaction() as conn:
            conn.execute(
                update(db.workers)
                .where(db.workers.c.id == self.worker_id)
                .values(heartbeat_at=self.clock(), status=status)
            )

    def bump(self, conn, scope, slot):
        counter = db.one(
            conn,
            select(db.counters).where(db.counters.c.scope == scope, db.counters.c.slot == slot),
        )
        if counter:
            generation = counter["generation"] + 1
            conn.execute(
                update(db.counters)
                .where(db.counters.c.id == counter["id"])
                .values(generation=generation)
            )
        else:
            generation = 1
            conn.execute(
                insert(db.counters).values(id=uid(), scope=scope, slot=slot, generation=generation)
            )
        return generation

    def claim(self):
        with self.database.transaction() as conn:
            at = self.clock()
            candidates = db.many(
                conn,
                select(db.tasks)
                .where(
                    db.tasks.c.status.in_(["QUEUED", "RETRY_WAIT"]),
                    db.tasks.c.next_attempt_at <= at,
                    db.tasks.c.scheduled_at <= at,
                )
                .order_by(db.tasks.c.next_attempt_at, db.tasks.c.created_at, db.tasks.c.id),
            )
            scoped_worker = (
                self.config.bilibili is not None
                and self.config.bilibili.b18_allowlist_file is not None
            )
            for task in candidates:
                if scoped_worker != ("b18_run_id" in task["snapshot"]):
                    continue
                if task["cancel_requested_at"] is not None:
                    task_state(conn, task, "CANCELLED", at)
                    continue
                if (
                    task["status"] == "QUEUED"
                    and task["attempt_count"] == 0
                    and task["execution_type"] == "scheduled"
                    and at - task["scheduled_at"] > task["max_lateness_seconds"]
                ):
                    task_state(conn, task, "EXPIRED", at)
                    continue
                if task["attempt_count"] >= task["retry_policy"]["max_attempts"]:
                    continue
                account = db.get(conn, db.accounts, task["account_id"])
                if (
                    account["status"] != "ACTIVE"
                    or not account["enabled"]
                    or db.one(
                        conn,
                        select(db.account_blocks).where(
                            db.account_blocks.c.account_id == account["id"]
                        ),
                    )
                ):
                    continue
                account_scope, task_scope = "account:" + account["id"], "task:" + task["id"]
                occupied = set(
                    conn.execute(
                        select(db.leases.c.slot).where(
                            db.leases.c.scope == account_scope, db.leases.c.released_at.is_(None)
                        )
                    ).scalars()
                )
                slot = next(
                    (s for s in range(account["concurrency_limit"]) if s not in occupied), None
                )
                if slot is None:
                    continue
                if db.one(
                    conn,
                    select(db.leases).where(
                        db.leases.c.scope == task_scope, db.leases.c.released_at.is_(None)
                    ),
                ):
                    continue
                fence, account_fence = (
                    self.bump(conn, task_scope, 0),
                    self.bump(conn, account_scope, slot),
                )
                attempt = dict(
                    id=uid(),
                    task_id=task["id"],
                    attempt_number=task["attempt_count"] + 1,
                    worker_id=self.worker_id,
                    status="CREATED",
                    phase="claimed",
                    execution_id=uid(),
                    fence=fence,
                    execution={},
                    exit_confirmed=False,
                    host_sequence=0,
                    recovery_generation=0,
                    started_at=at,
                    heartbeat_at=at,
                )
                conn.execute(insert(db.attempts).values(**attempt))
                for scope, index, generation in [
                    (task_scope, 0, fence),
                    (account_scope, slot, account_fence),
                ]:
                    conn.execute(
                        insert(db.leases).values(
                            id=uid(),
                            scope=scope,
                            slot=index,
                            attempt_id=attempt["id"],
                            generation=generation,
                            expires_at=at + self.config.lease_seconds,
                        )
                    )
                task_state(
                    conn,
                    task,
                    "CLAIMED",
                    at,
                    attempt_count=attempt["attempt_number"],
                    current_attempt_id=attempt["id"],
                    fence=fence,
                )
                return db.get(conn, db.attempts, attempt["id"])
        return None

    def owned(self, conn, original, recovery=False):
        at = self.clock()
        attempt = db.get(conn, db.attempts, original["id"])
        task = db.get(conn, db.tasks, attempt["task_id"])
        valid = (
            attempt["status"] in ACTIVE_ATTEMPTS
            and task["current_attempt_id"] == attempt["id"]
            and task["fence"] == original["fence"]
        )
        if recovery:
            valid = (
                valid
                and attempt["recovery_owner"] == self.worker_id
                and attempt["recovery_generation"] == original["recovery_generation"]
                and attempt["recovery_expires"] > at
            )
        else:
            leases = db.many(
                conn,
                select(db.leases).where(
                    db.leases.c.attempt_id == attempt["id"], db.leases.c.released_at.is_(None)
                ),
            )
            valid = (
                valid
                and attempt["worker_id"] == self.worker_id
                and attempt["recovery_owner"] is None
                and len(leases) == 2
                and all(lease["expires_at"] > at for lease in leases)
            )
        if not valid:
            raise Problem("execution_fenced", "执行所有权已失效，禁止提交或授权新动作", 409)
        return task, attempt

    def heartbeat(self, attempt, recovery=False):
        with self.database.transaction() as conn:
            at = self.clock()
            self.owned(conn, attempt, recovery)
            if recovery:
                conn.execute(
                    update(db.attempts)
                    .where(db.attempts.c.id == attempt["id"])
                    .values(recovery_expires=at + self.config.lease_seconds)
                )
            else:
                conn.execute(
                    update(db.leases)
                    .where(
                        db.leases.c.attempt_id == attempt["id"], db.leases.c.released_at.is_(None)
                    )
                    .values(expires_at=at + self.config.lease_seconds)
                )
                conn.execute(
                    update(db.attempts)
                    .where(db.attempts.c.id == attempt["id"])
                    .values(heartbeat_at=at)
                )

    def starting(self, original, execution):
        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original)
            if task["cancel_requested_at"]:
                return False
            task_state(conn, task, "VALIDATING", self.clock())
            transition(attempt["status"], "STARTING", attempt=True)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(status="STARTING", phase="execution_intent", execution=execution)
            )
            db.add_event(
                conn,
                task["id"],
                "attempt.starting",
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=self.clock(),
            )
            return True

    def process_identity(self, original, execution):
        with self.database.transaction() as conn:
            self.owned(conn, original)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == original["id"])
                .values(execution=execution, phase="process_recorded")
            )

    def permit(self, original):
        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original)
            if task["cancel_requested_at"]:
                return False
            if attempt["host_sequence"] != 1 or attempt["permission_at"] is not None:
                raise Problem("invalid_handshake", "未收到有效首次握手", 409)
            task_state(conn, task, "RUNNING", self.clock())
            transition(attempt["status"], "RUNNING", attempt=True)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(status="RUNNING", phase="permission_saved", permission_at=self.clock())
            )
            return True

    def host_event(self, original, message):
        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original)
            if message.host_sequence <= attempt["host_sequence"]:
                return False
            if message.host_sequence != attempt["host_sequence"] + 1:
                raise Problem("host_sequence", "宿主事件序号不连续", 409)
            if (
                message.type == "hello"
                and (message.host_sequence != 1 or attempt["permission_at"] is not None)
            ) or (message.type != "hello" and attempt["permission_at"] is None):
                raise Problem("host_phase", "宿主消息阶段无效", 409)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(host_sequence=message.host_sequence, phase=message.phase or message.type)
            )
            db.add_event(
                conn,
                task["id"],
                "host." + message.type,
                message.model_dump(exclude_none=True),
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=self.clock(),
            )
            return True

    def bilibili_hello(self, original, message):
        from upload_platform.adapters.bilibili.protocol import verify_identity

        with self.database.transaction() as conn:
            _, attempt = self.owned(conn, original)
            verify_identity(message, attempt)
            if message.type != "hello" or attempt["handed_off_at"] is not None:
                raise Problem("invalid_handshake", "引擎握手阶段无效", 409)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(execution={**attempt["execution"], "hello_received": True})
            )

    def handoff(self, original):
        from upload_platform.adapters.bilibili.b18 import B18Allowlist, denied

        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original)
            if task["cancel_requested_at"] is not None:
                return False
            permit = B18Allowlist.load(self.config)
            if permit:
                permit.check_task(task, executing=True)
                if attempt["attempt_number"] != 1:
                    raise denied("本轮 B18 仅允许首次执行")
                if task["snapshot"]["mode"] == "multipart":
                    permit.check_handoff(conn, task)
            elif "b18_run_id" in task["snapshot"]:
                raise denied("当前 Worker 未配置本轮 B18 清单")
            if (
                not attempt["execution"].get("hello_received")
                or attempt["handed_off_at"] is not None
            ):
                raise Problem("invalid_handshake", "引擎未完成握手或任务已经交接", 409)
            at = self.clock()
            task_state(conn, task, "RUNNING", at)
            transition(attempt["status"], "RUNNING", attempt=True)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(status="RUNNING", phase="handed_off", handed_off_at=at)
            )
            db.add_event(
                conn,
                task["id"],
                "attempt.handed_off",
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=at,
            )
            return True

    def bilibili_event(self, original, message):
        from upload_platform.adapters.bilibili.protocol import verify_identity

        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original)
            verify_identity(message, attempt)
            if attempt["handed_off_at"] is None or message.type != "progress":
                raise Problem("host_phase", "未交接执行不能报告进度", 409)
            observed = dict(attempt["execution"].get("observed_events", {}))
            key = str(message.sequence)
            value = message.model_dump()
            if key in observed:
                if observed[key] != value:
                    raise Problem("host_sequence", "同序号事件内容冲突", 409)
                return
            if message.sequence <= attempt["host_sequence"]:
                raise Problem("host_sequence", "阶段事件序号倒退", 409)
            observed[key] = value
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(
                    host_sequence=message.sequence,
                    phase=message.phase,
                    execution={**attempt["execution"], "observed_events": observed},
                )
            )
            db.add_event(
                conn,
                task["id"],
                "engine.progress",
                {"sequence": message.sequence, "phase": message.phase},
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=self.clock(),
            )

    def recovery_claim(self):
        with self.database.transaction() as conn:
            at = self.clock()
            candidates = db.many(
                conn,
                select(db.attempts)
                .where(db.attempts.c.status.in_(ACTIVE_ATTEMPTS))
                .order_by(db.attempts.c.started_at),
            )
            for attempt in candidates:
                if attempt["recovery_owner"]:
                    if attempt["recovery_expires"] > at:
                        continue
                else:
                    expired = db.one(
                        conn,
                        select(db.leases).where(
                            db.leases.c.attempt_id == attempt["id"],
                            db.leases.c.released_at.is_(None),
                            db.leases.c.expires_at <= at,
                        ),
                    )
                    if not expired:
                        continue
                task = db.get(conn, db.tasks, attempt["task_id"])
                if task["status"] != "RECOVERY_PENDING":
                    task_state(conn, task, "RECOVERY_PENDING", at)
                values = {
                    "recovery_owner": self.worker_id,
                    "recovery_generation": attempt["recovery_generation"] + 1,
                    "recovery_expires": at + self.config.lease_seconds,
                }
                conn.execute(
                    update(db.attempts).where(db.attempts.c.id == attempt["id"]).values(**values)
                )
                db.add_event(
                    conn,
                    task["id"],
                    "recovery.claimed",
                    {"generation": values["recovery_generation"]},
                    task_id=task["id"],
                    attempt_id=attempt["id"],
                    at=at,
                )
                return {**attempt, **values}
        return None

    def recovery_note(self, original, reason):
        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original, True)
            if attempt["error"] != reason:
                conn.execute(
                    update(db.attempts)
                    .where(db.attempts.c.id == attempt["id"])
                    .values(error=reason)
                )
                db.add_event(
                    conn,
                    task["id"],
                    "recovery.blocked",
                    {"reason": reason},
                    task_id=task["id"],
                    attempt_id=attempt["id"],
                    at=self.clock(),
                )

    def confirm_exit(self, original, proof, *, recovery=False):
        """Persist an observed exit before losing the live Job handle; retain both slots."""
        if proof not in ("job_empty_and_host_exited", "previous_boot_exited"):
            raise Problem("exit_unconfirmed", "缺少可靠执行树退出证据", 409)
        with self.database.transaction() as conn:
            task, attempt = self.owned(conn, original, recovery)
            if attempt["exit_confirmed"]:
                return
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(
                    exit_confirmed=True,
                    execution={**attempt["execution"], "exit_proof": proof},
                )
            )
            db.add_event(
                conn,
                task["id"],
                "attempt.exit_confirmed",
                {"proof": proof},
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=self.clock(),
            )

    def finish(self, original, outcome, *, proof, result=None, error=None, recovery=False):
        if not proof:
            raise Problem("exit_unconfirmed", "没有执行树退出证据，不能释放名额", 409)
        with self.database.transaction() as conn:
            at = self.clock()
            task, attempt = self.owned(conn, original, recovery)
            bilibili = task["snapshot"]["target"]["platform"] == "bilibili"
            authorized = attempt["handed_off_at"] if bilibili else attempt["permission_at"]
            if bilibili:
                if attempt["execution"] and not attempt["exit_confirmed"]:
                    raise Problem("exit_unconfirmed", "必须独立确认引擎进程树退出", 409)
                if authorized is not None and (
                    outcome == "cancelled"
                    or (result is None and outcome in ("retryable", "permanent", "succeeded"))
                ):
                    raise Problem("unsafe_outcome", "已交接执行缺少安全收尾证据", 409)
            if result is not None:
                if bilibili:
                    from upload_platform.adapters.bilibili.protocol import verify_result

                    parsed = verify_result(result, attempt)
                else:
                    from upload_platform.execution.protocol import HostResult, verify_identity

                    parsed = HostResult.model_validate(result)
                    verify_identity(parsed, attempt)
                    if authorized is None or parsed.host_sequence < attempt["host_sequence"]:
                        raise Problem("invalid_result", "结果缺少有效许可或已过时", 409)
                if parsed.outcome != outcome:
                    raise Problem("invalid_result", "结果不能被改写为其他结论", 409)
            if (
                bilibili
                and result
                and (result.get("error") or {}).get("code") == "CREDENTIAL_INVALID"
            ):
                from upload_platform.application.accounts import schedule_renewal

                schedule_renewal(conn, task["account_id"], attempt["execution"], at)
            if outcome == "succeeded":
                if result is None or authorized is None:
                    raise Problem("invalid_success", "成功缺少许可与结果证据", 409)
                state, attempt_state = "SUCCEEDED", "SUCCEEDED"
                conn.execute(
                    insert(db.receipts).values(
                        id=uid(),
                        task_id=task["id"],
                        attempt_id=attempt["id"],
                        source="adapter",
                        fake=not bilibili,
                        evidence=result["receipt"],
                        review_status=result["receipt"].get("review_status") if bilibili else None,
                        last_query_at=result["receipt"].get("queried_at") if bilibili else None,
                    )
                )
            elif outcome == "retryable":
                state = (
                    "RETRY_WAIT"
                    if task["attempt_count"] < task["retry_policy"]["max_attempts"]
                    else "FAILED"
                )
                attempt_state = "RETRYABLE_FAILURE"
                if task["cancel_requested_at"] is not None:
                    state, attempt_state = "CANCELLED", "CANCELLED"
            elif outcome == "permanent":
                state, attempt_state = "FAILED", "PERMANENT_FAILURE"
            elif outcome == "cancelled":
                state, attempt_state = "CANCELLED", "CANCELLED"
            else:
                state, attempt_state = "UNCERTAIN", "UNCERTAIN"
            changes = {}
            if state == "RETRY_WAIT":
                changes["next_attempt_at"] = (
                    at + task["retry_policy"]["retry_delays_seconds"][task["attempt_count"] - 1]
                )
            task_state(
                conn, task, state, at, payload={"exit_proof": proof, "error": error}, **changes
            )
            transition(attempt["status"], attempt_state, attempt=True)
            conn.execute(
                update(db.attempts)
                .where(db.attempts.c.id == attempt["id"])
                .values(
                    status=attempt_state,
                    phase="finished",
                    exit_confirmed=True,
                    ended_at=at,
                    result=result,
                    error=error,
                )
            )
            conn.execute(
                update(db.leases)
                .where(db.leases.c.attempt_id == attempt["id"], db.leases.c.released_at.is_(None))
                .values(released_at=at)
            )
            db.add_event(
                conn,
                task["id"],
                "attempt.finished",
                {"status": attempt_state, "exit_proof": proof},
                task_id=task["id"],
                attempt_id=attempt["id"],
                at=at,
            )
            if state == "SUCCEEDED" and task["cancel_requested_at"] is not None:
                db.add_event(
                    conn,
                    task["id"],
                    "task.cancel_not_effective",
                    task_id=task["id"],
                    attempt_id=attempt["id"],
                    at=at,
                )
