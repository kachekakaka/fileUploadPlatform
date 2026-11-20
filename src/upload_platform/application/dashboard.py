from sqlalchemy import func, select, tuple_
from pydantic import Field

from upload_platform.application.contracts import StrictModel
from upload_platform.domain.core import DEFINITE, Problem, timestamp
from upload_platform.infrastructure import database as db


class TaskRow(StrictModel):
    id: str
    work_id: str
    unit_id: str
    target_id: str
    ledger_id: str
    account_id: str
    previous_task_id: str | None
    submission_round: int
    status: str
    version: int
    snapshot: dict
    execution_type: str
    scheduled_at: float
    max_lateness_seconds: int
    retry_policy: dict
    attempt_count: int
    next_attempt_at: float
    current_attempt_id: str | None
    fence: int
    cancel_requested_at: float | None
    created_at: float = Field(description="Unix time in seconds")
    updated_at: float
    completed_at: float | None


class TaskAction(StrictModel):
    allowed: bool
    reason: str | None


class TaskDetail(TaskRow):
    attempt: dict | None
    receipt: dict | None
    evidence_ref: str | None = None
    available_actions: dict[str, TaskAction]


class TaskPage(StrictModel):
    items: list[TaskRow]
    next_cursor: str | None


class Dashboard:
    def __init__(self, service, login):
        self.service = service
        self.database = service.database
        self.config = self.database.config
        self.login = login

    def platforms(self):
        deployment = self.config.bilibili
        login = self.login.capabilities()["gui_login"]
        engine, error = {}, None
        if deployment is None:
            error = "engine_not_configured"
        else:
            try:
                engine = deployment.capabilities()
                if engine.get("upload_ready") is not True or not (
                    engine.get("verified_publish_modes")
                    or (deployment.allow_self_only and engine.get("supports_self_only") is True)
                ):
                    error = "upload_not_open"
            except Problem as exc:
                error = exc.code
        with self.database.read() as conn:
            accounts = db.many(conn, select(db.accounts))
            blocks = set(conn.execute(select(db.account_blocks.c.account_id)).scalars())

        def available(platform):
            return sum(
                account["platform"] == platform
                and account["enabled"]
                and account["status"] == "ACTIVE"
                and account["id"] not in blocks
                for account in accounts
            )

        return [
            {
                "id": "bilibili",
                "implemented": True,
                "configured": deployment is not None,
                "enabled": error is None,
                "reason": error,
                "available_accounts": available("bilibili"),
                "engine_build": engine.get("engine_build_id"),
                "capabilities": {
                    "login": login["available"],
                    "login_reason": login["reason"],
                    "multipart": True,
                    "self_only": bool(
                        deployment
                        and not error
                        and engine.get("supports_self_only") is True
                        and (
                            deployment.allow_self_only
                            or "self_only" in engine.get("verified_publish_modes", [])
                        )
                    ),
                    "hide_in_space": bool(
                        deployment
                        and not error
                        and engine.get("supports_hide_in_space") is True
                        and (
                            deployment.allow_hide_in_space
                            or engine.get("verified_hide_in_space") is True
                        )
                    ),
                    "remote_query": True,
                },
            },
            {
                "id": "fake",
                "implemented": True,
                "configured": True,
                "enabled": True,
                "reason": None,
                "available_accounts": available("fake"),
                "engine_build": None,
                "capabilities": {"login": False, "multipart": True, "remote_query": False},
            },
        ]

    def workers(self, conn):
        at = self.service.clock()
        return [
            {
                **worker,
                "active": worker["status"] == "RUNNING"
                and worker["heartbeat_at"] > at - self.config.lease_seconds,
            }
            for worker in db.many(conn, select(db.workers))
        ]

    def overview(self, version):
        with self.database.read() as conn:
            counts = dict(
                conn.execute(
                    select(db.tasks.c.status, func.count()).group_by(db.tasks.c.status)
                ).all()
            )
            accounts = conn.execute(select(func.count()).select_from(db.accounts)).scalar_one()
            recent = db.many(conn, select(db.tasks).order_by(db.tasks.c.created_at.desc()).limit(8))
            incidents = db.many(
                conn,
                select(db.tasks)
                .where(db.tasks.c.status.in_(["FAILED", "UNCERTAIN", "EXPIRED"]))
                .order_by(db.tasks.c.updated_at.desc())
                .limit(5),
            )
            workers = self.workers(conn)
        return {
            "version": version,
            "api": "ready",
            "database": "ready",
            "data_directory": str(self.config.data_dir),
            "manifest_roots": [str(p) for p in self.service.manifest_files.manifest_roots],
            "asset_roots": [str(p) for p in self.service.manifest_files.asset_roots],
            "workers": workers,
            "worker_active": any(worker["active"] for worker in workers),
            "accounts": accounts,
            "task_counts": counts,
            "terminal_tasks": sum(counts.get(state, 0) for state in DEFINITE),
            "recent_tasks": recent,
            "incidents": incidents,
            "platforms": self.platforms(),
        }

    def tasks(self, limit, cursor, order, work_id, status, account_id, platform, since, until):
        start = timestamp(since) if since is not None else None
        stop = timestamp(until) if until is not None else None
        if start is not None and stop is not None and start > stop:
            raise Problem("invalid_time", "起始时间须早于结束时间")
        tasks, targets = db.tasks.c, db.targets.c
        query = select(db.tasks).join(db.targets, targets.id == tasks.target_id)
        for column, value in (
            (tasks.work_id, work_id),
            (tasks.status, status),
            (tasks.account_id, account_id),
            (targets.platform, platform),
        ):
            if value is not None:
                query = query.where(column == value)
        if start is not None:
            query = query.where(tasks.created_at >= start)
        if stop is not None:
            query = query.where(tasks.created_at <= stop)
        with self.database.read() as conn:
            if order is None:
                if cursor:
                    query = query.where(tasks.id > cursor)
                query = query.order_by(tasks.id)
            else:
                newest = order == "newest"
                if cursor:
                    pivot = db.one(conn, select(db.tasks).where(tasks.id == cursor))
                    if pivot is None:
                        raise Problem("invalid_cursor", "任务列表游标无效")
                    position = tuple_(tasks.created_at, tasks.id)
                    value = (pivot["created_at"], pivot["id"])
                    query = query.where(position < value if newest else position > value)
                query = query.order_by(
                    *(
                        column.desc() if newest else column.asc()
                        for column in (tasks.created_at, tasks.id)
                    )
                )
            rows = db.many(conn, query.limit(limit + 1))
        return {
            "items": rows[:limit],
            "next_cursor": rows[limit - 1]["id"] if len(rows) > limit else None,
        }
