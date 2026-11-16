from typing import Literal
from uuid import UUID
import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from upload_platform.domain.core import timestamp, utc


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, allow_inf_nan=False)


class Metadata(StrictModel):
    title: str = Field(default=None, min_length=1)
    description: str | None = None
    tags: list[str] | None = None
    cover: str | None = None

    @field_validator("title")
    @classmethod
    def title_not_empty(cls, value):
        if value is None or not value.strip():
            raise ValueError("显式标题不能为空")
        return value

    @field_validator("cover")
    @classmethod
    def cover_not_empty(cls, value):
        if value is not None and not value.strip():
            raise ValueError("封面引用不能为空字符串")
        return value

    @field_validator("description")
    @classmethod
    def clear_description(cls, value):
        return value or ""

    @field_validator("tags")
    @classmethod
    def clear_tags(cls, value):
        return value or []


class WorkMetadata(Metadata):
    id: str
    ready: bool = False

    @field_validator("id")
    @classmethod
    def valid_uuid(cls, value):
        return str(UUID(value))


class Asset(StrictModel):
    id: str = Field(min_length=1)
    path: str = Field(min_length=1)


class Video(Asset):
    subtitles: list[str] = Field(default_factory=list)
    metadata: Metadata = Field(default_factory=Metadata)


class Subtitle(Asset):
    language: str = Field(min_length=1)


class Assets(StrictModel):
    videos: list[Video] = Field(default_factory=list)
    covers: list[Asset] = Field(default_factory=list)
    subtitles: list[Subtitle] = Field(default_factory=list)


class FakeOptions(StrictModel):
    scenario: Literal[
        "success",
        "retryable",
        "permanent",
        "crash",
        "cancel_safe",
        "cancel_uncertain",
        "invalid_result",
        "late_result",
        "child",
        "hang",
        "no_handshake",
        "crash_before_go",
    ] = "success"
    duration_seconds: float = Field(default=0.2, ge=0, le=300)
    failures_before_success: int = Field(default=0, ge=0)


class Target(StrictModel):
    platform: Literal["fake"] = "fake"
    account: str = "fake-default"
    enabled: bool = True
    options: FakeOptions = Field(default_factory=FakeOptions)


class Unit(StrictModel):
    id: str = Field(min_length=1)
    mode: Literal["multipart", "independent"]
    metadata_overrides: Metadata = Field(default_factory=Metadata)
    videos: list[str] = Field(min_length=1)
    targets: list[Target] = Field(default_factory=list)


class Manifest(StrictModel):
    version: Literal[1]
    work: WorkMetadata
    assets: Assets
    upload_units: list[Unit] = Field(min_length=1)

    @field_validator("version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("version 必须为整数")
        return value

    @model_validator(mode="after")
    def references(self):
        groups = [self.assets.videos, self.assets.covers, self.assets.subtitles]
        ids = [a.id for group in groups for a in group]
        if len(ids) != len(set(ids)):
            raise ValueError("素材 ID 必须在作品内唯一")
        units = [u.id for u in self.upload_units]
        if len(units) != len(set(units)):
            raise ValueError("单元 ID 重复")
        videos, covers, subs = [set(a.id for a in g) for g in groups]
        for metadata in [
            self.work,
            *[u.metadata_overrides for u in self.upload_units],
            *[v.metadata for v in self.assets.videos],
        ]:
            if metadata.cover is not None and metadata.cover not in covers:
                raise ValueError("封面引用不存在或类型错误")
        for v in self.assets.videos:
            if not set(v.subtitles) <= subs or len(v.subtitles) != len(set(v.subtitles)):
                raise ValueError("字幕引用不存在或重复")
        for unit in self.upload_units:
            if not set(unit.videos) <= videos or len(unit.videos) != len(set(unit.videos)):
                raise ValueError("视频引用不存在或重复")
            targets = [(t.platform, t.account) for t in unit.targets if t.enabled]
            if len(targets) != len(set(targets)):
                raise ValueError("单元内启用目标重复")
        return self

    def normalized(self):
        result = self.model_dump(mode="json")
        # Overrides must distinguish absent (inherit) from explicit null (clear).
        result["work"] = self.work.model_dump(mode="json", exclude_unset=True)
        result["work"]["ready"] = self.work.ready
        for field, default in (("description", ""), ("tags", []), ("cover", None)):
            result["work"].setdefault(field, default)
        for unit, value in zip(self.upload_units, result["upload_units"]):
            value["metadata_overrides"] = unit.metadata_overrides.model_dump(exclude_unset=True)
        for video, value in zip(self.assets.videos, result["assets"]["videos"]):
            value["metadata"] = video.metadata.model_dump(exclude_unset=True)
        return result


class RetryPolicy(StrictModel):
    max_attempts: int = Field(default=3, ge=1, le=100)
    retry_delays_seconds: list[int] = Field(default_factory=lambda: [30, 120])

    @model_validator(mode="after")
    def delays(self):
        if len(self.retry_delays_seconds) != self.max_attempts - 1 or any(
            x < 0 for x in self.retry_delays_seconds
        ):
            raise ValueError("重试间隔必须非负且长度为 max_attempts - 1")
        return self


class Registration(StrictModel):
    directory: str
    manifest: Manifest


class Submission(StrictModel):
    execution_type: Literal["immediate", "scheduled"] = "immediate"
    scheduled_at: str | None = None
    max_lateness_seconds: int = Field(default=86400, ge=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    directory: str | None = None
    manifest: Manifest | None = None

    @model_validator(mode="after")
    def execution(self):
        if self.execution_type == "scheduled":
            if self.scheduled_at is None:
                raise ValueError("定时任务需要 scheduled_at")
            self.scheduled_at = utc(timestamp(self.scheduled_at))
        elif (
            "scheduled_at" in self.model_fields_set
            or "max_lateness_seconds" in self.model_fields_set
        ):
            raise ValueError("立即任务不能提供定时时间或迟到配置")
        if self.directory is not None and self.manifest is None:
            raise ValueError("提供 directory 时必须同时提供 manifest")
        return self


class ResolutionRequest(StrictModel):
    conclusion: Literal["succeeded", "failed", "uncertain"]
    reason: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    expected_task_version: int = Field(ge=1)

    @field_validator("reason")
    @classmethod
    def nonblank(cls, value):
        if not value.strip():
            raise ValueError("需要非空理由")
        return value


class ScheduleRequest(StrictModel):
    enabled: bool = True
    parent_directory: str
    kind: Literal["daily", "weekly"]
    local_time: str
    timezone: str = "Asia/Shanghai"
    weekday: int | None = Field(default=None, ge=1, le=7)
    max_lateness_seconds: int = Field(default=86400, ge=0)
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)

    @model_validator(mode="after")
    def time_rule(self):
        from datetime import time
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        if not re.fullmatch(r"\d{2}:\d{2}:\d{2}", self.local_time):
            raise ValueError("local_time 使用 HH:mm:ss")
        time.fromisoformat(self.local_time)
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("未知 IANA 时区") from exc
        if (self.kind == "weekly") != (self.weekday is not None):
            raise ValueError("仅每周规则必须提供 weekday")
        return self


class SchedulePatch(StrictModel):
    expected_version: int = Field(ge=1)
    enabled: bool | None = None
    parent_directory: str | None = None
    kind: Literal["daily", "weekly"] | None = None
    local_time: str | None = None
    timezone: str | None = None
    weekday: int | None = Field(default=None, ge=1, le=7)
    max_lateness_seconds: int | None = Field(default=None, ge=0)
    retry_policy: RetryPolicy | None = None


class Empty(StrictModel):
    pass
