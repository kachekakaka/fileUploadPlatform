"""Local deployment approval for the two B18 submissions."""

import json
from pathlib import Path
from uuid import UUID

from pydantic import Field, field_validator, model_validator
from sqlalchemy import and_, or_, select

from upload_platform.application.contracts import BilibiliOptions, StrictModel
from upload_platform.domain.core import Problem
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import lexical_root, relative_path, root_path


def denied(reason):
    return Problem("b18_not_permitted", reason, 409)


def unique_fields(pairs):
    fields = dict(pairs)
    if len(fields) != len(pairs):
        raise ValueError("B18 清单含重复字段")
    return fields


class ApprovedPart(StrictModel):
    video_id: str = Field(min_length=1)
    path: str
    title: str = Field(min_length=1)

    _relative = field_validator("path")(relative_path)


class ApprovedWork(StrictModel):
    work_id: str
    directory: str
    unit_id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    description: str
    tags: list[str] = Field(min_length=1)
    cover: str | None
    parts: list[ApprovedPart]
    options: BilibiliOptions

    _root = field_validator("directory")(lexical_root)

    @field_validator("work_id")
    @classmethod
    def uuid_text(cls, value):
        return str(UUID(value))

    @field_validator("cover")
    @classmethod
    def cover_path(cls, value):
        return relative_path(value) if value is not None else None


class B18Supplement(StrictModel):
    single_task_id: str
    failed_task_id: str
    failed_attempt_id: str

    @field_validator("single_task_id", "failed_task_id", "failed_attempt_id")
    @classmethod
    def uuid_text(cls, value):
        return str(UUID(value))


class B18Allowlist(StrictModel):
    run_id: str
    core_data_dir: str
    engine_build_id: str = Field(min_length=1)
    mid: str = Field(pattern=r"^[1-9][0-9]*$")
    single: ApprovedWork | None = None
    multi: ApprovedWork | None = None
    supplement: B18Supplement | None = None
    self_only_trial: bool = False
    public_multi_trial: bool = False

    _root = field_validator("core_data_dir")(lexical_root)

    @field_validator("run_id")
    @classmethod
    def uuid_text(cls, value):
        return str(UUID(value))

    @model_validator(mode="after")
    def approved_works(self):
        if self.public_multi_trial:
            if (
                self.self_only_trial
                or self.single is not None
                or self.multi is None
                or self.supplement is not None
                or self.multi.options.hide_in_space
            ):
                raise ValueError("候选版补验只允许一份在空间列表中正常显示的公开多 P")
            works = ((self.multi, 2),)
        elif self.self_only_trial:
            if (
                self.single is None
                or self.multi is not None
                or self.supplement is not None
                or self.single.options.publish_mode != "self_only"
                or len(self.single.parts) != 1
            ):
                raise ValueError("仅自己可见验收只允许一份私密单 P")
            return self
        else:
            if self.single is None or self.multi is None:
                raise ValueError("公开试投须同时指定单 P 和多 P")
            if (
                self.single.work_id == self.multi.work_id
                or self.single.directory == self.multi.directory
            ):
                raise ValueError("两份作品需要不同的编号和目录")
            works = ((self.single, 1), (self.multi, 2))
        for work, count in works:
            if (
                work.options.publish_mode != "public"
                or len(work.parts) != count
                or len({part.video_id for part in work.parts}) != count
                or len({Path(part.path) for part in work.parts}) != count
            ):
                raise ValueError("公开试投稿的分 P 数量、素材引用或可见范围无效")
        return self

    @classmethod
    def load(cls, config):
        engine = config.bilibili
        if engine is None or engine.b18_allowlist_file is None:
            return None
        try:
            with engine.b18_allowlist_file.open("rb") as stream:
                raw = stream.read(256 * 1024 + 1)
            if len(raw) > 256 * 1024:
                raise ValueError("清单过大")
            permit = cls.model_validate(json.loads(raw, object_pairs_hook=unique_fields))
            permit_path = engine.b18_allowlist_file.resolve(strict=True)
            data_dir = lexical_root(str(config.data_dir.resolve(strict=True)))
        except (OSError, ValueError, RuntimeError, Problem) as exc:
            raise denied("本轮 B18 清单已撤下、不可读或格式无效") from exc
        if permit.core_data_dir != data_dir or permit.engine_build_id != engine.build_id:
            raise denied("B18 清单与当前 Core 数据目录或引擎构建不符")
        if any(
            work is not None and permit_path.is_relative_to(work.directory)
            for work in (permit.single, permit.multi)
        ):
            raise denied("B18 清单必须保存在作品目录之外")
        return permit

    def check_item(self, work_id, directory, item, paths):
        from upload_platform.adapters.bilibili.mapping import posting_fields

        target = item["target"]
        if target["platform"] != "bilibili" or target["expected_mid"] != self.mid:
            raise denied("仅允许清单指定的 Bilibili 账号")
        approved = self.single if item["mode"] == "independent" else self.multi
        if approved is None:
            if self.public_multi_trial:
                raise denied("本轮只开放指定的公开多 P")
            raise denied("本轮只开放指定的私密单 P")
        fields = posting_fields(item, paths)
        fields["options"] = {"hide_in_space": False, **fields["options"]}
        for part, reference in zip(fields["parts"], item["parts"], strict=True):
            part["video_id"] = reference["video_id"]
        actual = dict(
            fields, work_id=work_id, directory=lexical_root(directory), unit_id=item["unit_id"]
        )
        if actual != approved.model_dump():
            raise denied("作品编号、目录、上传单元、有序素材或投稿配置与 B18 清单不符")

    def check_prepared(self, prepared):
        if len(prepared["expanded"]) != 1:
            raise denied("B18 作品只允许一个启用的投稿目标和一份稿件")
        paths = {k: asset["relative_path"] for k, asset in prepared["assets"].items()}
        if self.supplement and prepared["expanded"][0]["mode"] != "multipart":
            raise denied("B18 补测只开放原多 P 作品")
        self.check_item(
            prepared["manifest"]["work"]["id"],
            prepared["directory"],
            prepared["expanded"][0],
            paths,
        )

    def check_task(self, task, *, executing=False, original=False):
        snapshot = task["snapshot"]
        previous = self.supplement.failed_task_id if self.supplement and not original else None
        if (
            snapshot.get("b18_run_id") != self.run_id
            or (not original and snapshot["target"]["engine_build_id"] != self.engine_build_id)
            or task["execution_type"] != "immediate"
            or task["retry_policy"]["max_attempts"] != 1
            or task["submission_round"] != (2 if previous else 1)
            or task["previous_task_id"] != previous
            or (previous and snapshot["mode"] != "multipart")
        ):
            raise denied("任务不符合本轮 B18 清单及投稿轮次")
        directory = snapshot["directory"]
        if executing:
            try:
                directory = str(root_path(directory))
            except Problem as exc:
                raise denied("B18 作品目录已不可用") from exc
        self.check_item(
            task["work_id"],
            directory,
            snapshot,
            {ref["asset_id"]: ref["relative_path"] for ref in snapshot["assets"]},
        )

    def require_single_query(self, conn, *, account_id=None):
        # The upload receipt starts at generation zero. Only a completed separate
        # query advances it. A failed newer observation must not release the multi-P slot.
        query = select(db.tasks).where(
            db.tasks.c.snapshot["b18_run_id"].as_string() == self.run_id,
            db.tasks.c.snapshot["mode"].as_string() == "independent",
        )
        if self.supplement:
            query = query.where(
                db.tasks.c.id == self.supplement.single_task_id,
                db.tasks.c.account_id == account_id,
            )
        task = db.one(conn, query)
        if task and task["status"] == "SUCCEEDED":
            self.check_task(task, original=self.supplement is not None)
            latest = db.one(
                conn,
                select(db.remote_queries)
                .where(db.remote_queries.c.task_id == task["id"])
                .order_by(db.remote_queries.c.generation.desc())
                .limit(1),
            )
            if (
                not latest
                or latest["status"] != "FINISHED"
                or not latest["result"]
                or latest["error"]
            ):
                raise denied("本轮单 P 的最新回查尚未成功")
            verified = conn.execute(
                select(db.receipts.c.id).where(
                    db.receipts.c.task_id == task["id"],
                    db.receipts.c.fake.is_(False),
                    db.receipts.c.source.in_(["adapter", "bilibili-query"]),
                    db.receipts.c.query_generation == latest["generation"],
                    db.receipts.c.query_error.is_(None),
                )
            ).first()
            # A query that first recovered an uncertain submission created the receipt.
            # It cannot also count as the independent read-back of that receipt.
            recovered_by_latest = conn.execute(
                select(db.events.c.id).where(
                    db.events.c.task_id == task["id"],
                    db.events.c.type == "task.succeeded",
                    db.events.c.payload["query_id"].as_string() == latest["id"],
                )
            ).first()
            if verified and not recovered_by_latest:
                return
        raise denied("先完成本轮单 P 受理和成功回查，再提交多 P")

    def require_failed_multi(self, conn, account_id):
        from upload_platform.adapters.bilibili.protocol import verify_result

        original = db.one(
            conn, select(db.tasks).where(db.tasks.c.id == self.supplement.failed_task_id)
        )
        attempt_id = self.supplement.failed_attempt_id
        if (
            not original
            or original["snapshot"]["mode"] != "multipart"
            or original["account_id"] != account_id
            or original["status"] != "FAILED"
            or original["attempt_count"] != 1
            or original["current_attempt_id"] != attempt_id
        ):
            raise denied("补测只接受清单指定的原失败多 P 任务")
        self.check_task(original, original=True)
        attempt = db.one(conn, select(db.attempts).where(db.attempts.c.id == attempt_id))
        if (
            not attempt
            or attempt["task_id"] != original["id"]
            or attempt["attempt_number"] != 1
            or attempt["status"] != "RETRYABLE_FAILURE"
            or not attempt["exit_confirmed"]
            or attempt["handed_off_at"] is None
            or attempt["execution"].get("engine_build_id")
            != original["snapshot"]["target"]["engine_build_id"]
            or db.one(
                conn,
                select(db.leases).where(
                    db.leases.c.attempt_id == attempt_id, db.leases.c.released_at.is_(None)
                ),
            )
            or db.one(conn, select(db.receipts).where(db.receipts.c.task_id == original["id"]))
        ):
            raise denied("原执行未确认退出、已有回执或缺少明确的未投稿结果")
        try:
            result = verify_result(attempt["result"], attempt)
        except (ValueError, KeyError, TypeError, Problem) as exc:
            raise denied("原执行结果无法核验") from exc
        if (
            result.submission_state != "not_submitted"
            or result.outcome != "retryable"
            or result.verified_mid != self.mid
            or not result.error
            or result.error.code != "VIDEO_UPLOAD_FAILED"
        ):
            raise denied("原执行没有本次补测要求的视频传输失败证据")
        return original

    def check_handoff(self, conn, task):
        if self.public_multi_trial:
            return
        if self.supplement:
            original = self.require_failed_multi(conn, task["account_id"])
            ledger = db.get(conn, db.ledgers, task["ledger_id"])
            if (
                task["ledger_id"] != original["ledger_id"]
                or ledger["current_task_id"] != task["id"]
            ):
                raise denied("补测任务与原任务的账本不一致")
        self.require_single_query(conn, account_id=task["account_id"])

    def check_slot(self, conn, prepared, execution, *, previous, from_schedule):
        expected = self.supplement.failed_task_id if self.supplement else None
        if (
            previous != expected
            or from_schedule
            or execution.execution_type != "immediate"
            or execution.retry_policy.max_attempts != 1
        ):
            raise denied("B18 只允许手动立即投稿一次，请按清单入口使用 --max-attempts 1")
        self.check_prepared(prepared)
        item = prepared["expanded"][0]
        if self.supplement:
            account_id = item["target"]["account_id"]
            self.require_failed_multi(conn, account_id)
            self.require_single_query(conn, account_id=account_id)
            return
        work_id = prepared["manifest"]["work"]["id"]
        # Under the submission's BEGIN IMMEDIATE, the existing task itself is
        # the durable reservation, including cancelled, failed and uncertain tasks.
        reserved = db.many(
            conn,
            select(db.tasks).where(
                or_(
                    and_(
                        db.tasks.c.snapshot["b18_run_id"].as_string() == self.run_id,
                        db.tasks.c.snapshot["mode"].as_string() == item["mode"],
                    ),
                    and_(
                        db.tasks.c.work_id == work_id,
                        db.tasks.c.account_id == item["target"]["account_id"],
                    ),
                )
            ),
        )
        if len(reserved) > 1:
            raise denied("该作品或名额已关联其他投稿任务")
        if reserved:
            self.check_task(reserved[0])
            if (
                reserved[0]["work_id"] != work_id
                or reserved[0]["account_id"] != item["target"]["account_id"]
            ):
                raise denied("该 B18 名额已使用")
        elif item["mode"] == "multipart" and not self.public_multi_trial:
            self.require_single_query(conn)
