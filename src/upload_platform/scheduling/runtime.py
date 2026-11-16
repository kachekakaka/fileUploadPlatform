from datetime import datetime, time, timedelta, timezone
import threading
from zoneinfo import ZoneInfo

from pydantic import ValidationError
from sqlalchemy import select, insert, update, or_
from sqlalchemy.exc import SQLAlchemyError

from upload_platform.application.contracts import ScheduleRequest, Submission
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import root_path, read_manifest


def next_fire(config, after):
    zone = ZoneInfo(config["timezone"])
    local = datetime.fromtimestamp(after, timezone.utc).astimezone(zone)
    date = local.date()
    clock = time.fromisoformat(config["local_time"])
    for offset in range(16):
        day = date + timedelta(days=offset)
        if config["kind"] == "weekly" and day.isoweekday() != config["weekday"]:
            continue
        candidate = datetime.combine(day, clock, zone).replace(fold=0)
        instant = candidate.timestamp()
        round_trip = datetime.fromtimestamp(instant, timezone.utc).astimezone(zone)
        # A gap does not round-trip; ambiguous wall times always use fold=0.
        if round_trip.replace(tzinfo=None) != candidate.replace(tzinfo=None):
            continue
        if instant > after:
            return instant
    raise Problem("invalid_schedule", "无法计算下一个计划点")


class ScheduleService:
    def __init__(self, service):
        self.service, self.database, self.clock = service, service.database, service.clock

    def create(self, key, request):
        def prepare():
            config = request.model_dump()
            config["parent_directory"] = str(root_path(config["parent_directory"]))
            return config

        def action(conn, config):
            at, identifier = self.clock(), uid()
            values = dict(
                id=identifier,
                version=1,
                enabled=config["enabled"],
                config=config,
                next_fire_at=next_fire(config, at) if config["enabled"] else None,
                created_at=at,
            )
            conn.execute(insert(db.schedules).values(**values))
            conn.execute(
                insert(db.schedule_versions).values(
                    id=uid(), schedule_id=identifier, version=1, config=config
                )
            )
            db.add_event(conn, identifier, "schedule.created", {"version": 1}, at=at)
            return values

        return self.service.mutate("POST:/schedules", key, request, prepare, action, 201)

    def patch(self, identifier, key, request):
        def prepare():
            with self.database.read() as conn:
                old = db.get(conn, db.schedules, identifier)
            supplied = request.model_dump(exclude_unset=True)
            supplied.pop("expected_version")
            config = ScheduleRequest.model_validate({**old["config"], **supplied}).model_dump()
            config["parent_directory"] = str(root_path(config["parent_directory"]))
            return config

        def action(conn, config):
            old = db.get(conn, db.schedules, identifier)
            if old["version"] != request.expected_version:
                raise Problem("version_conflict", "规则已经被修改，请重新读取", 409)
            at = self.clock()
            changed_time = any(
                config[field] != old["config"][field]
                for field in ("kind", "local_time", "timezone", "weekday")
            )
            following = old["next_fire_at"]
            if not config["enabled"]:
                following = None
            elif changed_time or not old["enabled"] or following is None:
                following = next_fire(config, at)
            values = dict(
                version=old["version"] + 1,
                enabled=config["enabled"],
                config=config,
                next_fire_at=following,
            )
            conn.execute(
                update(db.schedules)
                .where(db.schedules.c.id == identifier, db.schedules.c.version == old["version"])
                .values(**values)
            )
            conn.execute(
                insert(db.schedule_versions).values(
                    id=uid(), schedule_id=identifier, version=values["version"], config=config
                )
            )
            db.add_event(
                conn, identifier, "schedule.updated", {"version": values["version"]}, at=at
            )
            return {**old, **values}

        return self.service.mutate(
            f"PATCH:/schedules/{{schedule_id}}:{identifier}", key, request, prepare, action
        )

    def detail(self, identifier):
        with self.database.read() as conn:
            schedule = db.get(conn, db.schedules, identifier)
            schedule["versions"] = db.many(
                conn,
                select(db.schedule_versions)
                .where(db.schedule_versions.c.schedule_id == identifier)
                .order_by(db.schedule_versions.c.version),
            )
            return schedule

    def run_detail(self, identifier):
        with self.database.read() as conn:
            run = db.get(conn, db.runs, identifier)
            run["works"] = db.many(
                conn,
                select(db.run_works)
                .where(db.run_works.c.run_id == identifier)
                .order_by(db.run_works.c.directory),
            )
            run["tasks"] = db.many(
                conn,
                select(db.run_tasks, db.tasks.c.status)
                .join(db.tasks, db.tasks.c.id == db.run_tasks.c.task_id)
                .where(db.run_tasks.c.run_id == identifier),
            )
            return run


class Scheduler:
    def __init__(self, service, owner, lease_seconds=30):
        self.service, self.database, self.clock = service, service.database, service.clock
        self.owner, self.lease_seconds = owner, lease_seconds

    def evaluate(self):
        at = self.clock()
        with self.database.read() as conn:
            rules = db.many(
                conn,
                select(db.schedules).where(
                    db.schedules.c.enabled.is_(True), db.schedules.c.next_fire_at <= at
                ),
            )
        for rule in rules:
            planned, count = rule["next_fire_at"], 1
            following = next_fire(rule["config"], planned)
            while following <= at:
                planned, count = following, count + 1
                following = next_fire(rule["config"], planned)
            with self.database.transaction() as conn:
                at = self.clock()
                while following <= at:
                    planned, count = following, count + 1
                    following = next_fire(rule["config"], planned)
                changed = conn.execute(
                    update(db.schedules)
                    .where(
                        db.schedules.c.id == rule["id"],
                        db.schedules.c.version == rule["version"],
                        db.schedules.c.enabled.is_(True),
                        db.schedules.c.next_fire_at == rule["next_fire_at"],
                    )
                    .values(next_fire_at=following)
                )
                if changed.rowcount != 1:
                    continue
                identifier = uid()
                skipped = at - planned > rule["config"]["max_lateness_seconds"]
                conn.execute(
                    insert(db.runs).values(
                        id=identifier,
                        schedule_id=rule["id"],
                        version=rule["version"],
                        planned_at=planned,
                        started_at=at,
                        first_missed_at=rule["next_fire_at"],
                        merged_count=count,
                        config=rule["config"],
                        status="SKIPPED" if skipped else "PENDING",
                        generation=0,
                    )
                )
                db.add_event(
                    conn,
                    rule["id"],
                    "schedule.skipped" if skipped else "schedule.triggered",
                    {"run_id": identifier, "planned_at": planned, "merged_count": count},
                    at=at,
                )
                self.service.fault("schedule_before_commit")

    def claim_run(self):
        with self.database.transaction() as conn:
            at = self.clock()
            run = db.one(
                conn,
                select(db.runs)
                .where(
                    or_(
                        db.runs.c.status == "PENDING",
                        (db.runs.c.status == "DISPATCHING") & (db.runs.c.expires_at <= at),
                    )
                )
                .order_by(db.runs.c.planned_at, db.runs.c.id)
                .limit(1),
            )
            if run is None:
                return None
            changes = {
                "owner": self.owner,
                "generation": run["generation"] + 1,
                "expires_at": at + self.lease_seconds,
                "status": "DISPATCHING",
            }
            conn.execute(update(db.runs).where(db.runs.c.id == run["id"]).values(**changes))
            return {**run, **changes}

    def owned(self, conn, run):
        current = db.get(conn, db.runs, run["id"])
        if (
            current["status"] != "DISPATCHING"
            or current["owner"] != self.owner
            or current["generation"] != run["generation"]
            or current["expires_at"] <= self.clock()
        ):
            raise Problem("dispatch_fenced", "派发所有权已失效", 409)
        return current

    def renew(self, run):
        with self.database.transaction() as conn:
            self.owned(conn, run)
            conn.execute(
                update(db.runs)
                .where(db.runs.c.id == run["id"])
                .values(expires_at=self.clock() + self.lease_seconds)
            )

    def dispatch(self, run):
        stop = threading.Event()

        def heartbeat():
            while not stop.wait(self.lease_seconds / 3):
                try:
                    self.renew(run)
                except Exception:
                    stop.set()

        thread = threading.Thread(target=heartbeat, daemon=True)
        thread.start()
        try:
            self._dispatch(run)
        finally:
            stop.set()
            thread.join(timeout=self.lease_seconds)

    def _dispatch(self, run):
        if run["candidates"] is None:
            try:
                parent = root_path(run["config"]["parent_directory"])
                candidates = sorted(
                    [str(p) for p in parent.iterdir() if p.is_dir()],
                    key=lambda s: (s.casefold(), s),
                )
            except (OSError, Problem) as exc:
                with self.database.transaction() as conn:
                    self.owned(conn, run)
                    conn.execute(
                        update(db.runs)
                        .where(db.runs.c.id == run["id"])
                        .values(
                            status="COMPLETED_WITH_ERRORS",
                            error=f"父目录枚举失败: {type(exc).__name__}",
                        )
                    )
                return
            with self.database.transaction() as conn:
                current = self.owned(conn, run)
                if current["candidates"] is None:
                    conn.execute(
                        update(db.runs)
                        .where(db.runs.c.id == run["id"])
                        .values(candidates=candidates)
                    )
                else:
                    candidates = current["candidates"]
        else:
            candidates = run["candidates"]
        self.service.fault("candidates_saved")
        for directory in candidates:
            with self.database.read() as conn:
                done = db.one(
                    conn,
                    select(db.run_works).where(
                        db.run_works.c.run_id == run["id"], db.run_works.c.directory == directory
                    ),
                )
            if done:
                continue
            try:
                candidate_root = root_path(directory)
                parent_root = root_path(run["config"]["parent_directory"])
                if candidate_root.parent != parent_root:
                    raise Problem("invalid_path", "周期候选目录经重解析后不是父目录的直接子目录")
                raw = read_manifest(directory)
                # Validate schema even when not ready, but no media reads or submission marks.
                from upload_platform.application.contracts import Manifest

                manifest = Manifest.model_validate(raw)
                if not manifest.work.ready:
                    self.record_error(run, directory, "SKIPPED_NOT_READY", None)
                    continue
                prepared = self.service.prepare_work(directory, manifest)
                execution = Submission(retry_policy=run["config"]["retry_policy"])
                with self.database.transaction() as conn:
                    self.owned(conn, run)
                    response = self.service.submit_prepared(conn, prepared, execution)
                    result_id = uid()
                    conn.execute(
                        insert(db.run_works).values(
                            id=result_id,
                            run_id=run["id"],
                            directory=directory,
                            work_id=response["work_id"],
                            status="DISPATCHED",
                        )
                    )
                    for task in response["tasks"]:
                        conn.execute(
                            insert(db.run_tasks).values(
                                id=uid(),
                                run_id=run["id"],
                                run_work_id=result_id,
                                task_id=task["task_id"],
                                created=task["created"],
                            )
                        )
                    self.service.fault("dispatch_before_commit")
            except SQLAlchemyError:
                # Shared persistence failure: stop, preserve unprocessed candidates for takeover.
                raise
            except Problem as exc:
                if exc.code == "dispatch_fenced":
                    raise
                self.record_error(
                    run, directory, "ERROR", {"code": exc.code, "message": exc.message}
                )
            except (ValidationError, OSError, ValueError, RuntimeError) as exc:
                self.record_error(
                    run,
                    directory,
                    "ERROR",
                    {"code": type(exc).__name__, "message": "作品校验或派发失败"},
                )
        with self.database.transaction() as conn:
            self.owned(conn, run)
            errors = db.one(
                conn,
                select(db.run_works).where(
                    db.run_works.c.run_id == run["id"], db.run_works.c.status == "ERROR"
                ),
            )
            conn.execute(
                update(db.runs)
                .where(db.runs.c.id == run["id"])
                .values(status="COMPLETED_WITH_ERRORS" if errors else "COMPLETED")
            )
            db.add_event(
                conn,
                run["schedule_id"],
                "schedule.dispatched",
                {"run_id": run["id"]},
                at=self.clock(),
            )

    def record_error(self, run, directory, status, error):
        with self.database.transaction() as conn:
            self.owned(conn, run)
            conn.execute(
                insert(db.run_works).values(
                    id=uid(), run_id=run["id"], directory=directory, status=status, error=error
                )
            )

    def tick(self):
        self.evaluate()
        run = self.claim_run()
        if run:
            self.dispatch(run)
        return run
