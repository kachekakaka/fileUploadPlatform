from copy import deepcopy
from datetime import datetime, timezone

import pytest
from pydantic import ValidationError

from upload_platform.application.contracts import (
    Manifest,
    Metadata,
    RetryPolicy,
    ScheduleRequest,
    Submission,
)
from upload_platform.application.manifest import validate_manifest
from upload_platform.domain.core import (
    Problem,
    TASK_TRANSITIONS,
    ATTEMPT_TRANSITIONS,
    aggregate,
    timestamp,
    transition,
)
from upload_platform.infrastructure.files import (
    parse_yaml,
    prepare,
    relative_path,
    lexical_root,
    asset_info,
)
from upload_platform.scheduling.runtime import next_fire


@pytest.mark.parametrize("attempt,table", [(False, TASK_TRANSITIONS), (True, ATTEMPT_TRANSITIONS)])
def test_all_state_edges(attempt, table):
    for old in table:
        for new in table:
            if new in table[old]:
                transition(old, new, attempt=attempt)
            else:
                with pytest.raises(Problem):
                    transition(old, new, attempt=attempt)


@pytest.mark.parametrize(
    "states,status",
    [
        ([], "NOT_SUBMITTED"),
        (["SUCCEEDED"], "SUCCEEDED"),
        (["FAILED", "SUCCEEDED"], "PARTIAL_SUCCESS"),
        (["CANCELLED", "EXPIRED"], "FINISHED_WITHOUT_SUCCESS"),
        (["FAILED", "QUEUED"], "IN_PROGRESS"),
        (["RUNNING", "UNCERTAIN"], "NEEDS_ATTENTION"),
    ],
)
def test_aggregation(states, status):
    assert aggregate(states)["status"] == status


def test_metadata_modes_and_bindings(work_factory):
    directory, manifest = work_factory()
    independent = deepcopy(manifest["upload_units"][0])
    independent.update(id="clips", mode="independent")
    manifest["upload_units"].append(independent)
    result = validate_manifest(str(directory), manifest)
    group, first, second = result["expanded"]
    assert group["metadata"] == {
        "title": "作品标题",
        "description": "简介",
        "tags": ["单元"],
        "cover": "cover",
    }
    assert [p["title"] for p in group["parts"]] == ["第一 P", "第二 P"]
    assert first["metadata"]["title"] == "第一 P"
    assert first["metadata"]["description"] == "独立简介"
    assert second["metadata"]["description"] == "简介"
    assert first["sources"]["title"] == "video"
    assert group["sources"]["title"] == "work"
    assert group["parts"][0]["subtitles"] == [{"id": "zh", "language": "zh-CN"}]
    assert group["parts"][1]["subtitles"] == []


def test_metadata_clear_replace_and_title_fallback(work_factory):
    directory, manifest = work_factory(independent=True)
    manifest["upload_units"][0]["metadata_overrides"] = {
        "description": None,
        "tags": [],
        "cover": None,
    }
    manifest["assets"]["videos"][0]["metadata"] = {}
    result = validate_manifest(str(directory), manifest)
    first = result["expanded"][0]
    assert first["metadata"] == {"title": "作品标题", "description": "", "tags": [], "cover": None}
    assert first["parts"][0]["title"] == "first"


@pytest.mark.parametrize(
    "override", [{"title": None}, {"title": " "}, {"cover": ""}, {"tags": "bad"}, {"unknown": 1}]
)
def test_metadata_rejects_invalid(override):
    with pytest.raises(ValidationError):
        Metadata.model_validate(override)


@pytest.mark.parametrize(
    "case",
    [
        "version",
        "unknown",
        "asset_duplicate",
        "unit_duplicate",
        "video_duplicate",
        "cover_reference",
        "subtitle_reference",
        "target_duplicate",
        "target_options",
        "empty_videos",
        "unknown_platform",
        "ready_type",
    ],
)
def test_manifest_rejections(work_factory, case):
    _, m = work_factory()
    if case == "version":
        m["version"] = 2
    elif case == "unknown":
        m["unexpected"] = True
    elif case == "asset_duplicate":
        m["assets"]["covers"][0]["id"] = "v1"
    elif case == "unit_duplicate":
        m["upload_units"].append(deepcopy(m["upload_units"][0]))
    elif case == "video_duplicate":
        m["upload_units"][0]["videos"] = ["v1", "v1"]
    elif case == "cover_reference":
        m["work"]["cover"] = "v1"
    elif case == "subtitle_reference":
        m["assets"]["videos"][0]["subtitles"] = ["cover"]
    elif case == "target_duplicate":
        m["upload_units"][0]["targets"] *= 2
    elif case == "target_options":
        m["upload_units"][0]["targets"][0]["options"]["unsupported"] = 1
    elif case == "empty_videos":
        m["upload_units"][0]["videos"] = []
    elif case == "unknown_platform":
        m["upload_units"][0]["targets"][0]["platform"] = "bilibili"
    else:
        m["work"]["ready"] = "true"
    with pytest.raises(ValidationError):
        Manifest.model_validate(m)


@pytest.mark.parametrize(
    "text",
    [
        "a: 1\na: 2",
        "a: !execute payload",
        "!!python/object/apply:os.system ['echo bad']",
        "1: value",
        "- array",
    ],
)
def test_safe_yaml(text):
    with pytest.raises(Problem):
        parse_yaml(text)


@pytest.mark.parametrize(
    "path",
    [
        "../bad",
        "a/../bad",
        "C:/bad",
        "C:bad",
        "/bad",
        "\\\\server\\bad",
        "a:stream",
        "a//b",
        "a. /b",
    ],
)
def test_relative_path_rejections(path):
    with pytest.raises(Problem):
        relative_path(path)


@pytest.mark.parametrize(
    "path", ["C:relative", "\\\\server\\share", "\\\\?\\C:\\path", "relative", "C:/path:stream"]
)
def test_root_rejections(path):
    with pytest.raises(Problem):
        lexical_root(path)


def test_prepare_ready_lock_and_failure(work_factory, monkeypatch):
    directory, manifest = work_factory(ready=False)
    del manifest["work"]["id"]
    from conftest import save_manifest

    save_manifest(directory, manifest)
    first = prepare(directory)
    assert first["work"]["ready"] is False
    assert prepare(directory)["work"]["id"] == first["work"]["id"]
    original = (directory / "upload.yaml").read_bytes()
    import upload_platform.infrastructure.files as files

    def fail(*_):
        raise OSError("injected write interruption")

    monkeypatch.setattr(files, "atomic_write", fail)
    with pytest.raises(OSError):
        prepare(directory, ready=True)
    assert (directory / "upload.yaml").read_bytes() == original
    assert not (directory / ".upload-platform-manifest.lock").exists()


def test_prepare_does_not_replace_foreign_lock(work_factory):
    directory, _ = work_factory()
    lock = directory / ".upload-platform-manifest.lock"
    lock.write_text("other operation")
    with pytest.raises(Problem):
        prepare(directory)
    assert lock.read_text() == "other operation"


def test_template_not_ready(tmp_path):
    value = prepare(tmp_path)
    assert value["work"]["ready"] is False
    assert value["upload_units"] == []
    with pytest.raises(Problem):
        prepare(tmp_path, ready=True)


def test_open_does_not_read_media(work_factory, monkeypatch):
    directory, _ = work_factory()
    import builtins

    actual = builtins.open

    class NoRead:
        def __init__(self, stream):
            self.stream = stream

        def fileno(self):
            return self.stream.fileno()

        def close(self):
            return self.stream.close()

        def read(self, *_):
            raise AssertionError("media scanned")

    def guarded(path, mode="r", **kwargs):
        stream = actual(path, mode, **kwargs)
        return NoRead(stream) if str(path).endswith(".mp4") else stream

    monkeypatch.setattr(builtins, "open", guarded)
    assert asset_info(str(directory), "first.mp4")["size"] == len(b"synthetic-fixture")


@pytest.mark.parametrize(
    "kwargs",
    [
        {"max_attempts": 0},
        {"max_attempts": 2},
        {"max_attempts": 2, "retry_delays_seconds": [-1]},
        {"max_attempts": True},
    ],
)
def test_retry_policy_invalid(kwargs):
    with pytest.raises(ValidationError):
        RetryPolicy(**kwargs)


def test_execution_time_contract():
    assert (
        Submission(
            execution_type="scheduled", scheduled_at="2026-11-16T08:00:00+08:00"
        ).scheduled_at
        == "2026-11-16T00:00:00Z"
    )
    for body in [
        {"scheduled_at": "2026-11-16T00:00:00Z"},
        {"max_lateness_seconds": 10},
        {"execution_type": "scheduled"},
    ]:
        with pytest.raises(ValidationError):
            Submission(**body)
    with pytest.raises(Problem):
        timestamp("2026-11-16T08:00:00")


def test_dst_and_weekly_next_fire(tmp_path):
    def rule(clock, **kw):
        return ScheduleRequest(
            parent_directory=str(tmp_path),
            kind="daily",
            local_time=clock,
            timezone="America/New_York",
            **kw,
        ).model_dump()

    gap = next_fire(rule("02:30:00"), timestamp("2026-03-08T00:00:00Z"))
    assert datetime.fromtimestamp(gap, timezone.utc).isoformat() == "2026-03-09T06:30:00+00:00"
    folded = rule("01:30:00")
    assert next_fire(folded, timestamp("2026-11-01T00:00:00Z")) == timestamp("2026-11-01T05:30:00Z")
    assert next_fire(folded, timestamp("2026-11-01T05:30:00Z")) == timestamp("2026-11-02T06:30:00Z")
    weekly = ScheduleRequest(
        parent_directory=str(tmp_path), kind="weekly", weekday=1, local_time="08:00:00"
    ).model_dump()
    assert next_fire(weekly, timestamp("2026-11-16T00:00:00Z")) == timestamp("2026-11-23T00:00:00Z")
