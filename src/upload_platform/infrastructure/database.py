from contextlib import contextmanager
from pathlib import Path
import re

from sqlalchemy import (
    MetaData,
    Table,
    Column,
    String,
    Integer,
    Float,
    JSON,
    Boolean,
    ForeignKey,
    UniqueConstraint,
    CheckConstraint,
    Index,
    create_engine,
    event,
    select,
    insert,
    func,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.schema import CreateTable, CreateIndex

from upload_platform.domain.core import Problem, uid, now, TASK_TRANSITIONS, ATTEMPT_TRANSITIONS


metadata = MetaData()


def col(name, kind=String, **kw):
    return Column(name, kind, **kw)


def ref(name, target, **kw):
    return Column(name, String, ForeignKey(target), **kw)


def table(name, *columns):
    return Table(name, metadata, col("id", primary_key=True), *columns)


schema_version = table("schema_version", col("version", Integer, nullable=False))
accounts = table(
    "accounts",
    col("platform", nullable=False),
    col("reference", nullable=False, unique=True),
    col("display_name", nullable=False),
    col("status", nullable=False),
    col("concurrency_limit", Integer, nullable=False),
    CheckConstraint("concurrency_limit > 0"),
)
roots = table("source_roots", col("directory", nullable=False, unique=True))
works = table(
    "upload_works",
    ref("root_id", "source_roots.id", nullable=False),
    col("manifest", JSON, nullable=False),
    col("created_at", Float, nullable=False),
    col("updated_at", Float, nullable=False),
)
units = table(
    "upload_units",
    ref("work_id", "upload_works.id", nullable=False),
    col("logical_id", nullable=False),
    col("mode", nullable=False),
    col("snapshot", JSON, nullable=False),
    CheckConstraint("mode IN ('multipart','independent')"),
)
assets = table(
    "assets",
    ref("work_id", "upload_works.id", nullable=False),
    ref("root_id", "source_roots.id", nullable=False),
    col("logical_id", nullable=False),
    col("relative_path", nullable=False),
    col("role", nullable=False),
    col("size", Integer, nullable=False),
    col("mtime_ns", Integer, nullable=False),
    CheckConstraint("size >= 0"),
)
unit_assets = table(
    "upload_unit_assets",
    ref("unit_id", "upload_units.id", nullable=False),
    ref("asset_id", "assets.id", nullable=False),
    col("position", Integer, nullable=False),
    UniqueConstraint("unit_id", "position"),
)
targets = table(
    "upload_targets",
    ref("unit_id", "upload_units.id", nullable=False),
    ref("account_id", "accounts.id", nullable=False),
    col("platform", nullable=False),
    col("enabled", Boolean, nullable=False),
    col("options", JSON, nullable=False),
)
ledgers = table(
    "submission_ledger",
    ref("work_id", "upload_works.id", nullable=False),
    col("unit_logical_id", nullable=False),
    col("mode", nullable=False),
    col("item_key", nullable=False),
    col("platform", nullable=False),
    ref("account_id", "accounts.id", nullable=False),
    ref("current_task_id", "tasks.id"),
    col("round", Integer, nullable=False),
    CheckConstraint("round >= 0"),
    UniqueConstraint("work_id", "unit_logical_id", "mode", "item_key", "platform", "account_id"),
)
tasks = table(
    "tasks",
    ref("work_id", "upload_works.id", nullable=False),
    ref("unit_id", "upload_units.id", nullable=False),
    ref("target_id", "upload_targets.id", nullable=False),
    ref("ledger_id", "submission_ledger.id", nullable=False),
    ref("account_id", "accounts.id", nullable=False),
    ref("previous_task_id", "tasks.id"),
    col("submission_round", Integer, nullable=False),
    col("status", nullable=False),
    col("version", Integer, nullable=False),
    col("snapshot", JSON, nullable=False),
    col("execution_type", nullable=False),
    col("scheduled_at", Float, nullable=False),
    col("max_lateness_seconds", Integer, nullable=False),
    col("retry_policy", JSON, nullable=False),
    col("attempt_count", Integer, nullable=False),
    col("next_attempt_at", Float, nullable=False),
    ref("current_attempt_id", "attempts.id"),
    col("fence", Integer, nullable=False),
    col("cancel_requested_at", Float),
    col("created_at", Float, nullable=False),
    col("updated_at", Float, nullable=False),
    col("completed_at", Float),
    UniqueConstraint("ledger_id", "submission_round"),
    CheckConstraint(
        "submission_round > 0 AND version > 0 AND attempt_count >= 0 AND fence >= 0 AND max_lateness_seconds >= 0"
    ),
    CheckConstraint("execution_type IN ('immediate','scheduled')"),
    CheckConstraint("status IN (" + ",".join(repr(x) for x in TASK_TRANSITIONS) + ")"),
)
task_assets = table(
    "task_assets",
    ref("task_id", "tasks.id", nullable=False),
    ref("asset_id", "assets.id", nullable=False),
    col("position", Integer, nullable=False),
    col("role", nullable=False),
    col("video_id"),
    UniqueConstraint("task_id", "position"),
)
workers = table(
    "workers",
    col("boot_id", nullable=False),
    col("pid", Integer, nullable=False),
    col("heartbeat_at", Float, nullable=False),
    col("status", nullable=False),
)
attempts = table(
    "attempts",
    ref("task_id", "tasks.id", nullable=False),
    col("attempt_number", Integer, nullable=False),
    ref("worker_id", "workers.id", nullable=False),
    col("status", nullable=False),
    col("phase", nullable=False),
    col("execution_id", nullable=False, unique=True),
    col("fence", Integer, nullable=False),
    col("execution", JSON, nullable=False),
    col("permission_at", Float),
    col("exit_confirmed", Boolean, nullable=False),
    col("host_sequence", Integer, nullable=False),
    col("result", JSON),
    col("error"),
    col("recovery_owner"),
    col("recovery_generation", Integer, nullable=False),
    col("recovery_expires", Float),
    col("started_at", Float, nullable=False),
    col("heartbeat_at", Float, nullable=False),
    col("ended_at", Float),
    UniqueConstraint("task_id", "attempt_number"),
    CheckConstraint(
        "attempt_number > 0 AND fence > 0 AND host_sequence >= 0 AND recovery_generation >= 0"
    ),
    CheckConstraint("status IN (" + ",".join(repr(x) for x in ATTEMPT_TRANSITIONS) + ")"),
)
Index(
    "one_active_attempt",
    attempts.c.task_id,
    unique=True,
    sqlite_where=attempts.c.status.in_(["CREATED", "STARTING", "RUNNING"]),
)
counters = table(
    "fencing_counters",
    col("scope", nullable=False),
    col("slot", Integer, nullable=False),
    col("generation", Integer, nullable=False),
    UniqueConstraint("scope", "slot"),
    CheckConstraint("generation > 0"),
)
leases = table(
    "leases",
    col("scope", nullable=False),
    col("slot", Integer, nullable=False),
    ref("attempt_id", "attempts.id", nullable=False),
    col("generation", Integer, nullable=False),
    col("expires_at", Float, nullable=False),
    col("released_at", Float),
    CheckConstraint("slot >= 0 AND generation > 0"),
)
Index(
    "one_occupied_slot",
    leases.c.scope,
    leases.c.slot,
    unique=True,
    sqlite_where=leases.c.released_at.is_(None),
)
events = table(
    "events",
    col("aggregate_id", nullable=False),
    col("sequence", Integer, nullable=False),
    ref("task_id", "tasks.id"),
    ref("attempt_id", "attempts.id"),
    col("type", nullable=False),
    col("payload", JSON, nullable=False),
    col("created_at", Float, nullable=False),
    UniqueConstraint("aggregate_id", "sequence"),
    CheckConstraint("sequence > 0"),
)
outbox = table(
    "outbox_events",
    ref("event_id", "events.id", nullable=False, unique=True),
    col("published_at", Float),
)
receipts = table(
    "receipts",
    ref("task_id", "tasks.id", nullable=False, unique=True),
    ref("attempt_id", "attempts.id", nullable=False),
    col("source", nullable=False),
    col("fake", Boolean, nullable=False),
    col("evidence", JSON, nullable=False),
)
resolutions = table(
    "resolutions",
    ref("task_id", "tasks.id", nullable=False),
    ref("attempt_id", "attempts.id", nullable=False),
    col("conclusion", nullable=False),
    col("reason", nullable=False),
    col("evidence_refs", JSON, nullable=False),
    col("source", nullable=False),
    col("created_at", Float, nullable=False),
)
idempotency = table(
    "idempotency_keys",
    col("scope", nullable=False),
    col("key", nullable=False),
    col("request", nullable=False),
    col("http_status", Integer, nullable=False),
    col("response", JSON, nullable=False),
    UniqueConstraint("scope", "key"),
)
schedules = table(
    "schedules",
    col("version", Integer, nullable=False),
    col("enabled", Boolean, nullable=False),
    col("config", JSON, nullable=False),
    col("next_fire_at", Float),
    col("created_at", Float, nullable=False),
    CheckConstraint("version > 0"),
)
schedule_versions = table(
    "schedule_versions",
    ref("schedule_id", "schedules.id", nullable=False),
    col("version", Integer, nullable=False),
    col("config", JSON, nullable=False),
    UniqueConstraint("schedule_id", "version"),
)
runs = table(
    "schedule_runs",
    ref("schedule_id", "schedules.id", nullable=False),
    col("version", Integer, nullable=False),
    col("planned_at", Float, nullable=False),
    col("started_at", Float, nullable=False),
    col("first_missed_at", Float, nullable=False),
    col("merged_count", Integer, nullable=False),
    col("config", JSON, nullable=False),
    col("status", nullable=False),
    col("candidates", JSON),
    col("owner"),
    col("generation", Integer, nullable=False),
    col("expires_at", Float),
    col("error"),
    UniqueConstraint("schedule_id", "version", "planned_at"),
    CheckConstraint(
        "status IN ('PENDING','DISPATCHING','COMPLETED','COMPLETED_WITH_ERRORS','SKIPPED')"
    ),
)
run_works = table(
    "schedule_run_works",
    ref("run_id", "schedule_runs.id", nullable=False),
    col("directory", nullable=False),
    ref("work_id", "upload_works.id"),
    col("status", nullable=False),
    col("error", JSON),
    UniqueConstraint("run_id", "directory"),
)
run_tasks = table(
    "schedule_run_tasks",
    ref("run_id", "schedule_runs.id", nullable=False),
    ref("run_work_id", "schedule_run_works.id", nullable=False),
    ref("task_id", "tasks.id", nullable=False),
    col("created", Boolean, nullable=False),
    UniqueConstraint("run_work_id", "task_id"),
)


def one(conn, statement):
    result = conn.execute(statement).mappings().first()
    return dict(result) if result is not None else None


def many(conn, statement):
    return [dict(row) for row in conn.execute(statement).mappings()]


def get(conn, relation, identifier):
    result = one(conn, select(relation).where(relation.c.id == identifier))
    if result is None:
        raise Problem("not_found", "资源不存在", 404)
    return result


def add_event(conn, aggregate_id, kind, payload=None, *, task_id=None, attempt_id=None, at=None):
    sequence = (
        conn.execute(
            select(func.coalesce(func.max(events.c.sequence), 0)).where(
                events.c.aggregate_id == aggregate_id
            )
        ).scalar_one()
        + 1
    )
    event_id = uid()
    conn.execute(
        insert(events).values(
            id=event_id,
            aggregate_id=aggregate_id,
            sequence=sequence,
            task_id=task_id,
            attempt_id=attempt_id,
            type=kind,
            payload=payload or {},
            created_at=now() if at is None else at,
        )
    )
    conn.execute(insert(outbox).values(id=uid(), event_id=event_id))
    return event_id


def _sql_normalize(sql):
    return re.sub(r"\s+", " ", sql.replace('"', "")).strip().lower()


class Database:
    """SQLAlchemy Core repository/UoW with explicit SQLite transaction control."""

    def __init__(self, config):
        self.config = config
        Path(config.database).parent.mkdir(parents=True, exist_ok=True)
        self.engine = create_engine(
            "sqlite:///" + str(config.database),
            connect_args={"check_same_thread": False},
            pool_pre_ping=True,
        )

        @event.listens_for(self.engine, "connect")
        def setup(connection, _):
            connection.isolation_level = None
            for pragma in (
                "foreign_keys=ON",
                "synchronous=FULL",
                f"busy_timeout={config.busy_timeout_ms}",
            ):
                connection.execute("PRAGMA " + pragma)

    @contextmanager
    def transaction(self):
        with self.engine.connect() as conn:
            try:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise

    @contextmanager
    def read(self):
        with self.engine.connect() as conn:
            conn.exec_driver_sql("BEGIN")
            try:
                yield conn
            finally:
                conn.rollback()

    def initialize(self):
        with self.transaction() as conn:
            existing = conn.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type IN ('table','index') AND name NOT LIKE 'sqlite_%'"
            ).all()
            if not existing:
                metadata.create_all(conn)
                conn.execute(insert(schema_version).values(id="core", version=1))
                conn.execute(
                    insert(accounts).values(
                        id=uid(),
                        platform="fake",
                        reference="fake-default",
                        display_name="Fake Account",
                        status="ACTIVE",
                        concurrency_limit=1,
                    )
                )
            else:
                actual = {name: _sql_normalize(sql) for name, sql in existing if sql}
                expected = {
                    t.name: _sql_normalize(str(CreateTable(t).compile(self.engine)))
                    for t in metadata.tables.values()
                }
                for t in metadata.tables.values():
                    expected.update(
                        {
                            i.name: _sql_normalize(str(CreateIndex(i).compile(self.engine)))
                            for i in t.indexes
                        }
                    )
                if actual != expected:
                    raise Problem(
                        "unsupported_database",
                        "数据库不是受支持的完整 Core schema；保留原库并停止",
                        503,
                    )
                version = one(conn, select(schema_version))
                if not version or version["version"] != 1:
                    raise Problem("unsupported_database", "数据库版本不受支持", 503)
                if conn.exec_driver_sql("PRAGMA foreign_key_check").first():
                    raise Problem("invalid_database", "数据库外键一致性检查失败", 503)
                fake = one(conn, select(accounts).where(accounts.c.reference == "fake-default"))
                if not fake or fake["platform"] != "fake":
                    raise Problem("invalid_database", "系统 Fake Account 缺失或损坏", 503)
        with self.engine.connect() as conn:
            if conn.exec_driver_sql("PRAGMA journal_mode=WAL").scalar_one().lower() != "wal":
                raise Problem("configuration", "数据库无法使用 WAL", 503)

    def close(self):
        self.engine.dispose()


def unavailable(exc):
    if isinstance(exc, OperationalError):
        return Problem("service_unavailable", "数据库暂不可用，请用原请求重试", 503)
    return exc
