import json
from pathlib import Path
from uuid import UUID

from sqlalchemy import select, insert, update

from upload_platform.application.contracts import (
    Registration,
    Submission,
    ResolutionRequest,
    SchedulePatch,
)
from upload_platform.application.manifest import validate_manifest
from upload_platform.domain.core import (
    Problem,
    uid,
    now,
    canonical,
    timestamp,
    aggregate,
    TERMINAL,
    DEFINITE,
    transition,
)
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import lexical_root, read_manifest


def normalized_request(model):
    # PATCH omission means retain; explicit null means clear (or fail validation).
    value = model.model_dump(mode="json")
    if isinstance(model, SchedulePatch):
        # Only top-level fields are partial; a supplied retry policy is a complete value.
        value = {name: value[name] for name in model.model_fields_set}
    if hasattr(model, "manifest") and model.manifest is not None:
        value["manifest"] = model.manifest.normalized()
    for key in ("directory", "parent_directory"):
        if value.get(key) is not None:
            value[key] = lexical_root(value[key])
    return value


def task_state(conn, task, state, at, *, payload=None, **changes):
    transition(task["status"], state)
    values = {"status": state, "version": task["version"] + 1, "updated_at": at, **changes}
    if state in TERMINAL:
        values["completed_at"] = at
    result = conn.execute(
        update(db.tasks)
        .where(db.tasks.c.id == task["id"], db.tasks.c.version == task["version"])
        .values(**values)
    )
    if result.rowcount != 1:
        raise Problem("version_conflict", "任务版本已经改变", 409)
    db.add_event(
        conn,
        task["id"],
        "task." + state.lower(),
        payload,
        task_id=task["id"],
        attempt_id=values.get("current_attempt_id", task["current_attempt_id"]),
        at=at,
    )
    return {**task, **values}


class Service:
    def __init__(self, database, clock=now, fault=None):
        self.database, self.clock = database, clock
        self.fault = fault or (lambda point: None)

    def replay(self, conn, scope, key, request):
        record = db.one(
            conn,
            select(db.idempotency).where(
                db.idempotency.c.scope == scope, db.idempotency.c.key == key
            ),
        )
        if record:
            if record["request"] != request:
                raise Problem("idempotency_conflict", "相同 Key 的请求正文不同", 409)
            return record["http_status"], record["response"]
        return None

    def mutate(self, scope, key, model, prepare, action, status=200):
        try:
            key = str(UUID(key))
        except (ValueError, TypeError, AttributeError) as exc:
            raise Problem("invalid_key", "修改请求必须提供 UUID Idempotency-Key", 400) from exc
        request = canonical(normalized_request(model))
        with self.database.read() as conn:
            existing = self.replay(conn, scope, key, request)
        if existing is not None:
            return existing
        prepared = prepare()
        with self.database.transaction() as conn:
            existing = self.replay(conn, scope, key, request)
            if existing is not None:
                return existing
            response = action(conn, prepared)
            self.fault("before_idempotency")
            conn.execute(
                insert(db.idempotency).values(
                    id=uid(),
                    scope=scope,
                    key=key,
                    request=request,
                    http_status=status,
                    response=response,
                )
            )
            self.fault("before_commit")
        self.fault("after_commit")
        return status, response

    def prepare_work(self, directory, manifest):
        prepared = validate_manifest(directory, manifest)
        with self.database.read() as conn:
            accounts = db.many(conn, select(db.accounts))
        for unit in prepared["manifest"]["upload_units"]:
            seen = set()
            for target in unit["targets"]:
                account = next(
                    (
                        a
                        for a in accounts
                        if target["account"] in (a["id"], a["reference"])
                        and a["platform"] == target["platform"]
                    ),
                    None,
                )
                if not account or account["status"] != "ACTIVE":
                    raise Problem("invalid_account", "目标账号不存在或不可用")
                identity = (target["platform"], account["id"])
                if target["enabled"] and identity in seen:
                    raise Problem("duplicate_target", "目标账号解析后重复")
                if target["enabled"]:
                    seen.add(identity)
                target["account_id"] = account["id"]
        # Expanded targets refer to the same objects in the normalized manifest.
        return prepared

    def save_work(self, conn, prepared):
        at, manifest = self.clock(), prepared["manifest"]
        work_id = manifest["work"]["id"]
        root = db.one(conn, select(db.roots).where(db.roots.c.directory == prepared["directory"]))
        if root is None:
            root = {"id": uid(), "directory": prepared["directory"]}
            conn.execute(insert(db.roots).values(**root))
        existing = db.one(conn, select(db.works).where(db.works.c.id == work_id))
        # Internal account IDs belong to the snapshot, not the public manifest.
        public = json.loads(canonical(manifest))
        for unit in public["upload_units"]:
            for target in unit["targets"]:
                target.pop("account_id", None)
        values = {"root_id": root["id"], "manifest": public, "updated_at": at}
        if existing:
            conn.execute(update(db.works).where(db.works.c.id == work_id).values(**values))
        else:
            conn.execute(insert(db.works).values(id=work_id, created_at=at, **values))
        asset_ids, unit_ids, target_ids = {}, {}, {}
        for logical, asset in prepared["assets"].items():
            asset_ids[logical] = uid()
            conn.execute(
                insert(db.assets).values(
                    id=asset_ids[logical],
                    work_id=work_id,
                    root_id=root["id"],
                    logical_id=logical,
                    relative_path=asset["relative_path"],
                    role=asset["role"],
                    size=asset["size"],
                    mtime_ns=asset["mtime_ns"],
                )
            )
        for unit in manifest["upload_units"]:
            unit_ids[unit["id"]] = unit_id = uid()
            conn.execute(
                insert(db.units).values(
                    id=unit_id,
                    work_id=work_id,
                    logical_id=unit["id"],
                    mode=unit["mode"],
                    snapshot=unit,
                )
            )
            for position, video in enumerate(unit["videos"]):
                conn.execute(
                    insert(db.unit_assets).values(
                        id=uid(), unit_id=unit_id, asset_id=asset_ids[video], position=position
                    )
                )
            for target in unit["targets"]:
                account = db.get(conn, db.accounts, target["account_id"])
                if account["status"] != "ACTIVE":
                    raise Problem("invalid_account", "目标账号已停用", 409)
                target_id = uid()
                if target["enabled"]:
                    target_ids[(unit["id"], target["platform"], account["id"])] = target_id
                conn.execute(
                    insert(db.targets).values(
                        id=target_id,
                        unit_id=unit_id,
                        account_id=account["id"],
                        platform=target["platform"],
                        enabled=target["enabled"],
                        options=target["options"],
                    )
                )
        db.add_event(conn, work_id, "work.registered", {"root_id": root["id"]}, at=at)
        return {
            "root_id": root["id"],
            "asset_ids": asset_ids,
            "unit_ids": unit_ids,
            "target_ids": target_ids,
        }

    @staticmethod
    def identity(work_id, item):
        return {
            "work_id": work_id,
            "unit_logical_id": item["unit_id"],
            "mode": item["mode"],
            "item_key": item["item_key"],
            "platform": item["target"]["platform"],
            "account_id": item["target"]["account_id"],
        }

    def ledger(self, conn, identity):
        return db.one(
            conn, select(db.ledgers).where(*[db.ledgers.c[k] == v for k, v in identity.items()])
        )

    def create_task(self, conn, item, ledger, prepared, saved, execution, previous=None):
        at, task_id = self.clock(), uid()
        planned = (
            timestamp(execution.scheduled_at) if execution.execution_type == "scheduled" else at
        )
        snapshot = {**item, "root_id": saved["root_id"], "directory": prepared["directory"]}
        snapshot["assets"] = [
            {**ref, "relative_path": prepared["assets"][ref["asset_id"]]["relative_path"]}
            for ref in item["assets"]
        ]
        account_id = item["target"]["account_id"]
        values = dict(
            id=task_id,
            work_id=ledger["work_id"],
            unit_id=saved["unit_ids"][item["unit_id"]],
            target_id=saved["target_ids"][
                (item["unit_id"], item["target"]["platform"], account_id)
            ],
            ledger_id=ledger["id"],
            account_id=account_id,
            previous_task_id=previous,
            submission_round=ledger["round"] + 1,
            status="QUEUED",
            version=1,
            snapshot=snapshot,
            execution_type=execution.execution_type,
            scheduled_at=planned,
            max_lateness_seconds=execution.max_lateness_seconds,
            retry_policy=execution.retry_policy.model_dump(),
            attempt_count=0,
            next_attempt_at=planned,
            fence=0,
            created_at=at,
            updated_at=at,
        )
        conn.execute(insert(db.tasks).values(**values))
        for position, ref in enumerate(item["assets"]):
            conn.execute(
                insert(db.task_assets).values(
                    id=uid(),
                    task_id=task_id,
                    asset_id=saved["asset_ids"][ref["asset_id"]],
                    position=position,
                    role=ref["role"],
                    video_id=ref["video_id"],
                )
            )
        conn.execute(
            update(db.ledgers)
            .where(db.ledgers.c.id == ledger["id"])
            .values(current_task_id=task_id, round=values["submission_round"])
        )
        db.add_event(
            conn,
            task_id,
            "task.queued",
            {"submission_round": values["submission_round"], "previous_task_id": previous},
            task_id=task_id,
            at=at,
        )
        self.fault("after_task")
        return {
            "task_id": task_id,
            "submission_round": values["submission_round"],
            "created": True,
            "status": "QUEUED",
            "snapshot": snapshot,
            "previous_task_id": previous,
        }

    def submit_prepared(self, conn, prepared, execution, *, previous=None):
        if not prepared["expanded"]:
            raise Problem("no_targets", "没有启用的上传目标")
        work_id = prepared["manifest"]["work"]["id"]
        items = prepared["expanded"]
        old = None
        if previous:
            old = db.get(conn, db.tasks, previous)
            ledger = db.get(conn, db.ledgers, old["ledger_id"])
            if old["status"] not in DEFINITE or ledger["current_task_id"] != previous:
                raise Problem(
                    "resubmit_conflict",
                    "只可重新提交结果明确的当前任务",
                    409,
                    {"current_task_id": ledger["current_task_id"]},
                )
            items = [
                item
                for item in items
                if all(ledger[k] == v for k, v in self.identity(work_id, item).items())
            ]
            if len(items) != 1:
                raise Problem("submission_removed", "原投稿的单元、模式、视频或目标已经移除或改变")
        saved = self.save_work(conn, prepared)
        response = []
        for item in items:
            identity = self.identity(work_id, item)
            ledger = self.ledger(conn, identity)
            if ledger and not previous:
                task = db.get(conn, db.tasks, ledger["current_task_id"])
                response.append(
                    {
                        "task_id": task["id"],
                        "submission_round": task["submission_round"],
                        "created": False,
                        "status": task["status"],
                        "snapshot": task["snapshot"],
                        "previous_task_id": task["previous_task_id"],
                    }
                )
                continue
            if ledger is None:
                ledger = {"id": uid(), **identity, "round": 0}
                conn.execute(insert(db.ledgers).values(**ledger))
            response.append(
                self.create_task(conn, item, ledger, prepared, saved, execution, previous)
            )
        return {"work_id": work_id, "tasks": response}

    def registration(self, key, request: Registration):
        def action(conn, prepared):
            saved = self.save_work(conn, prepared)
            return {"work_id": prepared["manifest"]["work"]["id"], "root_id": saved["root_id"]}

        return self.mutate(
            "POST:/works",
            key,
            request,
            lambda: self.prepare_work(request.directory, request.manifest),
            action,
            201,
        )

    def current_description(self, work_id, request=None):
        with self.database.read() as conn:
            work = db.get(conn, db.works, work_id)
            root = db.get(conn, db.roots, work["root_id"])
        directory = (
            request.directory if request and request.directory is not None else root["directory"]
        )
        manifest = (
            request.manifest
            if request and request.manifest is not None
            else read_manifest(directory)
        )
        prepared = self.prepare_work(directory, manifest)
        if prepared["manifest"]["work"]["id"] != work_id:
            raise Problem("work_id_mismatch", "manifest.work.id 必须与 URL 作品编号一致")
        return prepared

    def preview(self, work_id):
        prepared = self.current_description(work_id)
        with self.database.read() as conn:
            identities = set()
            for item in prepared["expanded"]:
                identity = self.identity(work_id, item)
                identities.add(canonical(identity))
                ledger = self.ledger(conn, identity)
                item["existing_task"] = (
                    db.get(conn, db.tasks, ledger["current_task_id"]) if ledger else None
                )
                item["submission_identity"] = identity
                item["submission_action"] = "reuse_current" if ledger else "create_new_submission"
            prior = db.many(conn, select(db.ledgers).where(db.ledgers.c.work_id == work_id))
            fields = ("work_id", "unit_logical_id", "mode", "item_key", "platform", "account_id")
            prepared["previously_submitted_not_in_preview"] = [
                {
                    **{key: ledger[key] for key in fields},
                    "current_task_id": ledger["current_task_id"],
                }
                for ledger in prior
                if canonical({key: ledger[key] for key in fields}) not in identities
            ]
        public = json.loads(canonical(prepared))
        for unit in public["manifest"]["upload_units"]:
            for target in unit["targets"]:
                target.pop("account_id", None)
        return public

    def submit(self, work_id, key, request: Submission, previous=None):
        scope = (
            f"POST:/tasks/{{task_id}}/resubmit:{previous}"
            if previous
            else f"POST:/works/{{work_id}}/submit:{work_id}"
        )
        return self.mutate(
            scope,
            key,
            request,
            lambda: self.current_description(work_id, request),
            lambda conn, prepared: self.submit_prepared(conn, prepared, request, previous=previous),
            201,
        )

    def cancel(self, task_id, key, request):
        def action(conn, _):
            task, at = db.get(conn, db.tasks, task_id), self.clock()
            if task["status"] not in TERMINAL:
                if task["status"] in ("QUEUED", "RETRY_WAIT"):
                    task = task_state(conn, task, "CANCELLED", at, cancel_requested_at=at)
                elif task["cancel_requested_at"] is None:
                    conn.execute(
                        update(db.tasks)
                        .where(db.tasks.c.id == task_id)
                        .values(cancel_requested_at=at, updated_at=at, version=task["version"] + 1)
                    )
                    db.add_event(
                        conn,
                        task_id,
                        "task.cancel_requested",
                        task_id=task_id,
                        attempt_id=task["current_attempt_id"],
                        at=at,
                    )
            task = db.get(conn, db.tasks, task_id)
            meaning = {
                "CANCELLED": "已取消",
                "SUCCEEDED": "已成功，取消未生效",
                "UNCERTAIN": "结果不确定，需人工核对",
            }.get(
                task["status"],
                "任务已经结束" if task["status"] in TERMINAL else "取消请求已记录，等待安全停止",
            )
            return {"task_id": task_id, "status": task["status"], "message": meaning}

        return self.mutate(
            f"POST:/tasks/{{task_id}}/cancel:{task_id}", key, request, lambda: None, action
        )

    def resolve(self, task_id, key, request: ResolutionRequest):
        def prepare():
            with self.database.read() as conn:
                task = db.get(conn, db.tasks, task_id)
                if task["status"] != "UNCERTAIN":
                    raise Problem("resolve_conflict", "仅可处置 UNCERTAIN", 409)
                attempt = db.get(conn, db.attempts, task["current_attempt_id"])
            if request.conclusion != "uncertain":
                ref = f"fake://{attempt['execution_id']}/observation"
                if not attempt["exit_confirmed"] or ref not in request.evidence_refs:
                    raise Problem(
                        "insufficient_evidence", "需要旧执行退出记录及对应模拟观察依据", 409
                    )
                try:
                    evidence = json.loads(
                        Path(attempt["execution"]["evidence_path"]).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError, KeyError) as exc:
                    raise Problem("insufficient_evidence", "模拟观察依据不可查询", 409) from exc
                if evidence != {
                    "fake": True,
                    "execution_id": attempt["execution_id"],
                    "outcome": request.conclusion,
                }:
                    raise Problem("insufficient_evidence", "模拟观察依据不支持该结论", 409)
            return attempt["id"]

        def action(conn, attempt_id):
            task = db.get(conn, db.tasks, task_id)
            attempt = db.get(conn, db.attempts, attempt_id)
            if (
                task["status"] != "UNCERTAIN"
                or task["version"] != request.expected_task_version
                or task["current_attempt_id"] != attempt_id
            ):
                raise Problem("version_conflict", "任务状态或版本已经变化", 409)
            if request.conclusion != "uncertain" and not attempt["exit_confirmed"]:
                raise Problem("exit_unconfirmed", "旧执行尚未确认退出", 409)
            resolution_id, at = uid(), self.clock()
            conn.execute(
                insert(db.resolutions).values(
                    id=resolution_id,
                    task_id=task_id,
                    attempt_id=attempt_id,
                    conclusion=request.conclusion,
                    reason=request.reason,
                    evidence_refs=request.evidence_refs,
                    source="manual-fake",
                    created_at=at,
                )
            )
            if request.conclusion != "uncertain":
                task = task_state(
                    conn,
                    task,
                    request.conclusion.upper(),
                    at,
                    payload={"resolution_id": resolution_id, "source": "manual-fake"},
                )
                if request.conclusion == "succeeded":
                    conn.execute(
                        insert(db.receipts).values(
                            id=uid(),
                            task_id=task_id,
                            attempt_id=attempt_id,
                            source="manual",
                            fake=True,
                            evidence=request.evidence_refs,
                        )
                    )
            else:
                conn.execute(
                    update(db.tasks)
                    .where(db.tasks.c.id == task_id)
                    .values(version=task["version"] + 1, updated_at=at)
                )
            db.add_event(
                conn,
                task_id,
                "task.resolved",
                {"resolution_id": resolution_id, "conclusion": request.conclusion},
                task_id=task_id,
                attempt_id=attempt_id,
                at=at,
            )
            return {"resolution_id": resolution_id, "task_id": task_id, "status": task["status"]}

        return self.mutate(
            f"POST:/tasks/{{task_id}}/resolve:{task_id}", key, request, prepare, action
        )

    def work(self, work_id):
        with self.database.read() as conn:
            work = db.get(conn, db.works, work_id)
            states = (
                conn.execute(
                    select(db.tasks.c.status)
                    .join(db.ledgers, db.ledgers.c.current_task_id == db.tasks.c.id)
                    .where(db.ledgers.c.work_id == work_id)
                )
                .scalars()
                .all()
            )
            return {
                **work,
                **aggregate(states),
                "directory": db.get(conn, db.roots, work["root_id"])["directory"],
            }

    def task(self, task_id):
        with self.database.read() as conn:
            task = db.get(conn, db.tasks, task_id)
            task["attempt"] = (
                db.get(conn, db.attempts, task["current_attempt_id"])
                if task["current_attempt_id"]
                else None
            )
            task["receipt"] = db.one(
                conn, select(db.receipts).where(db.receipts.c.task_id == task_id)
            )
            if task["attempt"]:
                task["evidence_ref"] = f"fake://{task['attempt']['execution_id']}/observation"
            return task

    def listing(self, relation, limit=50, cursor=None, **filters):
        if not 1 <= limit <= 200:
            raise Problem("invalid_page", "limit 范围为 1—200")
        statement = select(relation).where(
            *[relation.c[k] == v for k, v in filters.items() if v is not None]
        )
        if cursor:
            statement = statement.where(relation.c.id > cursor)
        with self.database.read() as conn:
            items = db.many(conn, statement.order_by(relation.c.id).limit(limit + 1))
        return {
            "items": items[:limit],
            "next_cursor": items[limit - 1]["id"] if len(items) > limit else None,
        }

    def event_page(self, task_id, after=0, limit=50):
        if after < 0 or not 1 <= limit <= 200:
            raise Problem("invalid_cursor", "事件游标或 limit 无效")
        with self.database.read() as conn:
            db.get(conn, db.tasks, task_id)
            items = db.many(
                conn,
                select(db.events)
                .where(db.events.c.task_id == task_id, db.events.c.sequence > after)
                .order_by(db.events.c.sequence)
                .limit(limit),
            )
        return {"items": items, "next_sequence": items[-1]["sequence"] if items else after}
