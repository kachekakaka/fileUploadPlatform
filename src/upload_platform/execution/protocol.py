from typing import Literal

from pydantic import Field, model_validator, field_validator

from upload_platform.application.contracts import StrictModel, FakeOptions
from upload_platform.domain.core import Problem


class Identity(StrictModel):
    protocol_version: Literal[1] = 1
    task_id: str
    attempt_id: str
    execution_id: str
    generation: int = Field(ge=1)

    @field_validator("protocol_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("protocol_version 必须为整数")
        return value


class HostRequest(Identity):
    type: Literal["request"] = "request"
    attempt_number: int = Field(ge=1)
    options: FakeOptions
    result_path: str
    evidence_path: str


class Command(Identity):
    type: Literal["go", "cancel"]


class HostEvent(Identity):
    type: Literal["hello", "progress", "result"]
    host_sequence: int = Field(ge=1)
    phase: str | None = None
    progress: int | None = Field(default=None, ge=0, le=100)
    result_path: str | None = None
    child_pid: int | None = None


class HostResult(Identity):
    type: Literal["result"] = "result"
    host_sequence: int = Field(ge=2)
    outcome: Literal["succeeded", "retryable", "permanent", "cancelled", "uncertain"]
    safe: bool
    fake: Literal[True] = True
    receipt: dict | None = None
    history: list[HostEvent]

    @model_validator(mode="after")
    def evidence(self):
        if len(self.history) < 2 or self.host_sequence != len(self.history) + 1:
            raise ValueError("终态序号与持久事件链不一致")
        for index, event in enumerate(self.history, 1):
            if event.host_sequence != index or event.type != (
                "hello" if index == 1 else "progress"
            ):
                raise ValueError("结果包含越序事件")
            if any(
                getattr(event, field) != getattr(self, field) for field in Identity.model_fields
            ):
                raise ValueError("结果事件链身份不一致")
        if self.outcome == "succeeded":
            if self.receipt != {
                "fake": True,
                "execution_id": self.execution_id,
                "remote_id": "fake-" + self.attempt_id,
            }:
                raise ValueError("模拟成功回执不合法")
        elif self.receipt is not None:
            raise ValueError("非成功结果不能携带成功回执")
        if self.outcome in ("retryable", "permanent", "cancelled") and not self.safe:
            raise ValueError("安全失败或取消需要明确安全证据")
        return self


def identity(attempt):
    return {
        "protocol_version": 1,
        "task_id": attempt["task_id"],
        "attempt_id": attempt["id"],
        "execution_id": attempt["execution_id"],
        "generation": attempt["fence"],
    }


def verify_identity(message, attempt):
    for key, value in identity(attempt).items():
        if getattr(message, key) != value:
            raise Problem("invalid_host_identity", "宿主消息版本、身份或执行代次无效", 409)
