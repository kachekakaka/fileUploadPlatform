from typing import Literal
from uuid import UUID

from pydantic import Field, model_validator, field_validator

from upload_platform.application.contracts import StrictModel
from upload_platform.domain.core import Problem


class Identity(StrictModel):
    protocol_version: Literal[1]
    platform: Literal["bilibili"]
    task_id: str
    attempt_id: str
    execution_id: str
    generation: int = Field(ge=1)
    expected_mid: str = Field(pattern=r"^[1-9][0-9]*$")

    @field_validator("protocol_version", mode="before")
    @classmethod
    def exact_version(cls, value):
        if type(value) is not int:
            raise ValueError("协议版本必须是整数")
        return value

    @field_validator("task_id", "attempt_id", "execution_id")
    @classmethod
    def uuid_string(cls, value):
        if str(UUID(value)) != value:
            raise ValueError("执行标识必须是规范 UUID")
        return value


class Message(StrictModel):
    identity: Identity
    engine_build_id: str
    type: Literal["hello", "progress", "result"]
    sequence: int = Field(ge=0)
    phase: (
        Literal[
            "request_accepted",
            "uploading",
            "waiting_submit",
            "submit_intent",
            "submit_response",
            "finished",
        ]
        | None
    ) = None
    at: float | None = None
    verified_mid: str | None = None
    details: dict | None = None
    result_path: str | None = None

    @model_validator(mode="after")
    def shape(self):
        if self.type == "hello" and (
            self.sequence != 0
            or self.phase is not None
            or self.result_path is not None
            or self.details is not None
        ):
            raise ValueError("握手不能携带执行结果")
        if self.type == "progress" and (
            self.sequence < 1
            or self.phase is None
            or self.at is None
            or self.details is None
            or self.result_path is not None
        ):
            raise ValueError("阶段证据不完整")
        if self.type == "result" and (
            self.sequence < 2
            or self.result_path is None
            or self.phase is not None
            or self.details is not None
        ):
            raise ValueError("结果通知不完整")
        if self.verified_mid is not None and self.verified_mid != self.identity.expected_mid:
            raise ValueError("经验证 MID 与预期不符")
        if self.type == "progress":
            expected = {
                "request_accepted": set(),
                "uploading": {"parts"},
                "waiting_submit": set(),
                "submit_intent": {"submit_api", "studio", "video_keys"},
                "submit_response": {"code", "aid", "bvid", "video_keys"},
                "finished": {"outcome", "submission_state"},
            }
            if set(self.details) != expected[self.phase]:
                raise ValueError("阶段证据含未知字段或缺字段")
            if (
                self.phase in ("uploading", "waiting_submit", "submit_intent", "submit_response")
                and self.verified_mid != self.identity.expected_mid
            ):
                raise ValueError("凭据未验证不能进入上传或投稿阶段")
        return self


class Receipt(StrictModel):
    platform: Literal["bilibili"]
    verified_mid: str
    aid: int | None = Field(default=None, ge=1)
    bvid: str | None = Field(default=None, pattern=r"^BV[A-Za-z0-9]{10}$")
    video_keys: list[str] = Field(min_length=1)
    submit_api: Literal["web"]
    submit_code: Literal[0]
    query_verified: Literal[True]
    queried_at: float
    review_status: int | str | None

    @field_validator("submit_code", "query_verified", mode="before")
    @classmethod
    def exact_types(cls, value, info):
        expected = bool if info.field_name == "query_verified" else int
        if type(value) is not expected:
            raise ValueError("受理证据字段类型错误")
        return value


class EngineError(StrictModel):
    code: str = Field(pattern=r"^[A-Z][A-Z0-9_]{0,63}$")


class Result(StrictModel):
    identity: Identity
    engine_build_id: str
    type: Literal["result"]
    sequence: int = Field(ge=3)
    verified_mid: str | None
    outcome: Literal["succeeded", "retryable", "permanent", "uncertain"]
    submission_state: Literal["accepted", "not_submitted", "unknown"]
    error: EngineError | None
    receipt: Receipt | None
    history: list[Message] = Field(min_length=2, max_length=1000)

    @model_validator(mode="after")
    def reliable_evidence(self):
        transitions = {
            None: {"request_accepted"},
            "request_accepted": {"uploading", "finished"},
            "uploading": {"waiting_submit", "finished"},
            "waiting_submit": {"submit_intent", "finished"},
            "submit_intent": {"submit_response", "finished"},
            "submit_response": {"finished"},
            "finished": set(),
        }
        phase = None
        by_phase = {}
        for index, event in enumerate(self.history, 1):
            if (
                event.type != "progress"
                or event.sequence != index
                or event.identity != self.identity
                or event.engine_build_id != self.engine_build_id
                or event.phase not in transitions[phase]
            ):
                raise ValueError("执行证据身份、顺序或阶段矛盾")
            phase = event.phase
            by_phase[phase] = event
        if phase != "finished" or self.sequence != len(self.history) + 1:
            raise ValueError("没有完整封存结果")
        if self.history[-1].details != {
            "outcome": self.outcome,
            "submission_state": self.submission_state,
        }:
            raise ValueError("封存结论不一致")
        if self.verified_mid is not None and self.verified_mid != self.identity.expected_mid:
            raise ValueError("结果 MID 不一致")
        if self.submission_state == "not_submitted":
            if (
                "submit_intent" in by_phase
                or self.outcome not in ("retryable", "permanent")
                or not self.error
                or self.receipt is not None
            ):
                raise ValueError("没有可靠未投稿证据")
        elif self.submission_state == "unknown":
            if self.outcome != "uncertain" or self.receipt is not None:
                raise ValueError("未知受理状态不能变成明确结果")
        else:
            response = by_phase.get("submit_response")
            intent = by_phase.get("submit_intent")
            receipt = self.receipt
            if (
                self.outcome != "succeeded"
                or self.error is not None
                or not receipt
                or not intent
                or not response
                or self.verified_mid != self.identity.expected_mid
            ):
                raise ValueError("成功缺少受理与查询证据")
            if receipt.verified_mid != self.verified_mid or (
                receipt.aid is None and receipt.bvid is None
            ):
                raise ValueError("成功缺少远端身份")
            if response.details != {
                "code": 0,
                "aid": receipt.aid,
                "bvid": receipt.bvid,
                "video_keys": receipt.video_keys,
            }:
                raise ValueError("响应与回执没有关联")
            if (
                intent.details.get("video_keys") != receipt.video_keys
                or [v.get("filename") for v in intent.details.get("studio", {}).get("videos", [])]
                != receipt.video_keys
            ):
                raise ValueError("远端视频与投稿意图不一致")
        return self


def identity(attempt):
    return {
        "protocol_version": 1,
        "platform": "bilibili",
        "task_id": attempt["task_id"],
        "attempt_id": attempt["id"],
        "execution_id": attempt["execution_id"],
        "generation": attempt["fence"],
        "expected_mid": attempt["execution"]["expected_mid"],
    }


def verify_identity(message, attempt):
    if (
        message.identity.model_dump() != identity(attempt)
        or message.engine_build_id != attempt["execution"]["engine_build_id"]
    ):
        raise Problem("invalid_host_identity", "引擎消息身份、MID 或构建版本不符", 409)


def verify_result(value, attempt):
    parsed = Result.model_validate(value)
    verify_identity(parsed, attempt)
    if attempt["handed_off_at"] is None or parsed.sequence < attempt["host_sequence"]:
        raise Problem("invalid_result", "结果没有有效交接记录或序号已过期", 409)
    for event in attempt["execution"].get("observed_events", {}).values():
        sequence = event["sequence"]
        if sequence > len(parsed.history) or parsed.history[sequence - 1].model_dump(
            exclude_none=False
        ) != Message.model_validate(event).model_dump(exclude_none=False):
            raise Problem("invalid_result", "同序号阶段证据冲突", 409)
    return parsed
