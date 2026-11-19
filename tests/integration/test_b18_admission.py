from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path
import subprocess

import pytest
from sqlalchemy import insert, select, update

from conftest import save_manifest
from helpers.b18_cases import prepare_cases, write_json
from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.application.contracts import Empty, Registration, ScheduleRequest, Submission
from upload_platform.application.remote_queries import RemoteQueries
from upload_platform.application.service import Service
from upload_platform.domain.core import Problem, uid
from upload_platform.execution.leases import ExecutionStore
from upload_platform.execution.runtime import Supervisor
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.files import read_manifest
from upload_platform.scheduling.runtime import Scheduler, ScheduleService


ONCE = {"max_attempts": 1, "retry_delays_seconds": []}


@pytest.fixture
def setup(tmp_path, config, clock, work_factory, monkeypatch):
    permit = prepare_cases(tmp_path, work_factory)
    engine = EngineConfig.load(tmp_path / "engine.json")
    database = db.Database(replace(config, data_dir=tmp_path / "data", bilibili=engine))
    database.initialize()
    with database.transaction() as conn:
        for mid in ("42", "43"):
            conn.execute(
                insert(db.accounts).values(
                    id=uid(),
                    reference="bilibili:" + mid,
                    platform="bilibili",
                    remote_user_id=mid,
                    display_name="synthetic",
                    status="ACTIVE",
                    concurrency_limit=3,
                    credential_reference=mid + "-" + uid() + ".json",
                )
            )
    monkeypatch.setenv("BUE_FIXTURE_CONTROL", str(tmp_path / "control.json"))
    try:
        yield Service(database, clock), permit
    finally:
        database.close()


def register(service, work):
    directory = work["directory"]
    service.registration(
        uid(), Registration(directory=directory, manifest=read_manifest(directory))
    )


def test_only_approved_account_files_and_metadata(setup, tmp_path):
    service, permit = setup
    work = permit["multi"]
    root = work["directory"]
    original = read_manifest(root)
    # Exercise rejection even when an otherwise compatible publish mode is generally enabled.
    caps_path = tmp_path / "capabilities.json"
    caps = json.loads(caps_path.read_text())
    write_json(
        caps_path, {**caps, "upload_ready": True, "verified_publish_modes": ["public", "self_only"]}
    )
    changes = [
        (("work", "id"), uid()),
        (("work", "title"), "other"),
        (("work", "description"), "other"),
        (("work", "tags"), ["other"]),
        (("work", "cover"), None),
        (("upload_units", 0, "id"), "new"),
        (("upload_units", 0, "mode"), "independent"),
        (("upload_units", 0, "videos"), ["v2", "v1"]),
        (("upload_units", 0, "videos"), ["v1"]),
        (("assets", "videos", 0, "path"), "second.mp4"),
        (("assets", "videos", 0, "metadata", "title"), "other"),
        (("assets", "covers", 0, "path"), "first.mp4"),
        (("upload_units", 0, "targets", 0, "account"), "bilibili:43"),
        (("upload_units", 0, "targets", 0, "options", "publish_mode"), "self_only"),
        (("upload_units", 0, "targets", 0, "options", "category_id"), 1),
        (("upload_units", 0, "targets", 0, "options", "hide_in_space"), True),
        (
            ("upload_units", 0, "targets", 0, "options"),
            {"category_id": 171, "copyright": 2, "source": "other", "publish_mode": "public"},
        ),
        (
            ("upload_units", 0, "targets"),
            original["upload_units"][0]["targets"] + [{"platform": "fake"}],
        ),
    ]
    for path, value in changes:
        manifest = deepcopy(original)
        target = manifest
        for component in path[:-1]:
            target = target[component]
        target[path[-1]] = value
        with pytest.raises(Problem) as error:
            service.registration(uid(), Registration(directory=root, manifest=manifest))
        assert error.value.code == "b18_not_permitted", path
    with pytest.raises(Problem, match="目录"):
        service.registration(
            uid(), Registration(directory=permit["single"]["directory"], manifest=original)
        )
    register(service, work)
    altered = deepcopy(original)
    altered["work"]["title"] += "changed"
    save_manifest(Path(root), altered)
    with pytest.raises(Problem, match="配置"):
        service.preview(work["work_id"])
    with pytest.raises(Problem, match="配置"):
        service.submit(work["work_id"], uid(), Submission(retry_policy=ONCE))
    with service.database.read() as conn:
        assert not db.many(conn, select(db.tasks))


def test_hide_in_space_cannot_use_an_engine_without_the_option(setup, tmp_path):
    service, permit = setup
    work = permit["multi"]
    work["options"]["hide_in_space"] = True
    write_json(tmp_path / "allowlist.json", permit)
    manifest = read_manifest(work["directory"])
    manifest["upload_units"][0]["targets"][0]["options"]["hide_in_space"] = True
    request = Registration(directory=work["directory"], manifest=manifest)
    with pytest.raises(Problem, match="不支持在个人空间"):
        service.registration(uid(), request)
    caps = tmp_path / "capabilities.json"
    write_json(caps, json.loads(caps.read_text()) | {"supports_hide_in_space": True})
    service.registration(uid(), request)


def test_self_only_trial_allows_one_private_single_with_supported_engine(setup, tmp_path):
    service, permit = setup
    multi = permit.pop("multi")
    work = permit["single"]
    permit["self_only_trial"] = True
    work["options"]["publish_mode"] = "self_only"
    write_json(tmp_path / "allowlist.json", permit)
    manifest = read_manifest(work["directory"])
    options = manifest["upload_units"][0]["targets"][0]["options"]
    options["publish_mode"] = "self_only"
    save_manifest(Path(work["directory"]), manifest)

    with pytest.raises(Problem, match="不支持仅自己可见"):
        register(service, work)
    caps = tmp_path / "capabilities.json"
    write_json(caps, json.loads(caps.read_text()) | {"supports_self_only": True})
    with pytest.raises(Problem, match="私密单 P"):
        register(service, multi)
    public_manifest = deepcopy(manifest)
    public_manifest["upload_units"][0]["targets"][0]["options"]["publish_mode"] = "public"
    with pytest.raises(Problem, match="与 B18 清单不符"):
        service.registration(
            uid(), Registration(directory=work["directory"], manifest=public_manifest)
        )
    register(service, work)
    request = Submission(retry_policy=ONCE)
    first = service.submit(work["work_id"], uid(), request)[1]["tasks"][0]
    repeated = service.submit(work["work_id"], uid(), request)[1]["tasks"][0]
    assert repeated["task_id"] == first["task_id"] and not repeated["created"]
    store = ExecutionStore(service.database)
    store.register()
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(first["task_id"])["status"] == "SUCCEEDED"
    evidence = tmp_path / "data" / "executions" / attempt["id"] / "fixture-received.json"
    assert json.loads(evidence.read_text())["request"]["options"]["publish_mode"] == "self_only"
    assert store.claim() is None


def test_public_multi_trial_allows_one_ordered_public_work_without_a_single(setup, tmp_path):
    service, permit = setup
    single = permit.pop("single")
    multi = permit["multi"]
    allowlist = tmp_path / "allowlist.json"
    permit["public_multi_trial"] = True
    for invalid in (
        {**permit, "single": single},
        {**permit, "self_only_trial": True},
        {**permit, "multi": {**multi, "options": {**multi["options"], "hide_in_space": True}}},
        {**permit, "multi": {**multi, "parts": multi["parts"][:1]}},
    ):
        write_json(allowlist, invalid)
        with pytest.raises(Problem, match="格式无效"):
            register(service, multi)
    write_json(allowlist, permit)
    with pytest.raises(Problem, match="只开放指定的公开多 P"):
        register(service, single)
    register(service, multi)
    manifest = read_manifest(multi["directory"])
    reversed_manifest = deepcopy(manifest)
    reversed_manifest["upload_units"][0]["videos"].reverse()
    with pytest.raises(Problem, match="有序素材"):
        service.submit(
            multi["work_id"], uid(), Submission(retry_policy=ONCE, manifest=reversed_manifest)
        )
    with pytest.raises(Problem, match="手动立即投稿一次"):
        service.submit(multi["work_id"], uid(), Submission())

    request = Submission(retry_policy=ONCE)
    [first] = service.submit(multi["work_id"], uid(), request)[1]["tasks"]
    [repeated] = service.submit(multi["work_id"], uid(), request)[1]["tasks"]
    assert repeated["task_id"] == first["task_id"] and not repeated["created"]
    store = ExecutionStore(service.database)
    store.register()
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(first["task_id"])["status"] == "SUCCEEDED"
    [evidence] = (tmp_path / "data" / "executions").glob("*/fixture-received.json")
    request = json.loads(evidence.read_text())["request"]
    assert request["options"]["publish_mode"] == "public"
    assert [part["title"] for part in request["parts"]] == [
        part["title"] for part in multi["parts"]
    ]
    assert store.claim() is None


def test_concurrent_requests_restart_and_used_quota(setup, tmp_path):
    service, permit = setup
    work = permit["single"]
    register(service, work)
    request = Submission(retry_policy=ONCE)
    key = uid()

    def submit(k):
        return service.submit(work["work_id"], k, request)[1]["tasks"][0]

    with ThreadPoolExecutor(4) as pool:
        responses = list(pool.map(submit, [key, key, uid(), uid()]))
    assert len({t["task_id"] for t in responses}) == 1
    task_id = responses[0]["task_id"]
    service.cancel(task_id, uid(), Empty())
    reopened = db.Database(service.database.config)
    try:
        reopened.initialize()
        second = Service(reopened)
        reused = second.submit(work["work_id"], uid(), request)[1]["tasks"][0]
        assert reused["task_id"] == task_id and not reused["created"]
        with pytest.raises(Problem, match="只允许手动"):
            second.submit(work["work_id"], uid(), request, previous=task_id)
        # Changing the approved ledger identity under an existing round cannot consume a second slot.
        work["unit_id"] = "renamed"
        manifest = read_manifest(work["directory"])
        manifest["upload_units"][0]["id"] = "renamed"
        save_manifest(Path(work["directory"]), manifest)
        write_json(tmp_path / "allowlist.json", permit)
        with pytest.raises(Problem):
            second.submit(work["work_id"], uid(), request)
        with reopened.read() as conn:
            assert [t["id"] for t in db.many(conn, select(db.tasks))] == [task_id]
    finally:
        reopened.close()


def test_manual_only_and_schedule_dispatch(setup, tmp_path, clock):
    service, permit = setup
    work = permit["single"]
    register(service, work)
    for request in (
        Submission(),
        Submission(
            execution_type="scheduled", scheduled_at="2026-11-18T00:00:00Z", retry_policy=ONCE
        ),
    ):
        with pytest.raises(Problem, match="只允许手动"):
            service.submit(work["work_id"], uid(), request)
    ScheduleService(service).create(
        uid(),
        ScheduleRequest(
            parent_directory=str(tmp_path / "works"),
            kind="daily",
            local_time="00:00:01",
            timezone="UTC",
            retry_policy=ONCE,
        ),
    )
    clock.value += 2
    Scheduler(service, "b18-test").tick()
    with service.database.read() as conn:
        assert not db.many(conn, select(db.tasks))
        results = db.many(conn, select(db.run_works))
        assert len(results) == 2 and all(row["status"] == "ERROR" for row in results)


@pytest.mark.parametrize(
    "change",
    [
        "missing",
        "malformed",
        "duplicate_mid",
        "build",
        "data_dir",
        "same_work",
        "three_parts",
        "private",
    ],
)
def test_invalid_deployment_fails_closed(setup, tmp_path, change):
    service, permit = setup
    path = tmp_path / "allowlist.json"
    if change == "missing":
        path.rename(tmp_path / "withdrawn.json")
    elif change == "malformed":
        path.write_text("{")
    elif change == "duplicate_mid":
        path.write_text('{"mid":"43",' + json.dumps(permit)[1:], encoding="utf-8")
    else:
        if change == "build":
            permit["engine_build_id"] += "-wrong"
        elif change == "data_dir":
            permit["core_data_dir"] = str(tmp_path / "elsewhere")
        elif change == "same_work":
            permit["multi"]["work_id"] = permit["single"]["work_id"]
        elif change == "three_parts":
            permit["multi"]["parts"].append(permit["multi"]["parts"][0])
        else:
            permit["single"]["options"]["publish_mode"] = "self_only"
        write_json(path, permit)
    with pytest.raises(Problem) as error:
        register(service, permit["single"])
    assert error.value.code == "b18_not_permitted"


def test_work_cannot_supply_its_own_allowlist(setup):
    service, permit = setup
    path = Path(permit["single"]["directory"]) / "allowlist.json"
    write_json(path, permit)
    config = service.database.config
    other = db.Database(replace(config, bilibili=replace(config.bilibili, b18_allowlist_file=path)))
    try:
        with pytest.raises(Problem, match="作品目录之外"):
            register(Service(other), permit["single"])
    finally:
        other.close()


@pytest.mark.parametrize("when", ["queued", "after_identity"])
def test_withdrawn_permit_cannot_reach_engine(setup, tmp_path, when):
    service, permit = setup
    register(service, permit["single"])
    task = service.submit(permit["single"]["work_id"], uid(), Submission(retry_policy=ONCE))[1][
        "tasks"
    ][0]

    def revoke(point):
        if point == when:
            (tmp_path / "allowlist.json").rename(tmp_path / "withdrawn.json")

    revoke("queued")
    store = ExecutionStore(service.database)
    store.register()
    attempt = store.claim()
    Supervisor(store, fault=revoke).execute(attempt)
    result = service.task(task["task_id"])
    assert result["status"] == "FAILED", result
    assert result["attempt"]["error"] == "b18_not_permitted"
    assert result["attempt"]["handed_off_at"] is None
    assert result["attempt"]["exit_confirmed"] and store.claim() is None
    directory = tmp_path / "data" / "executions" / attempt["id"]
    assert not (directory / "fixture-received.json").exists()


def test_changed_work_directory_cannot_reach_engine(setup, tmp_path):
    service, permit = setup
    single = permit["single"]
    register(service, single)
    task = service.submit(single["work_id"], uid(), Submission(retry_policy=ONCE))[1]["tasks"][0]
    directory = Path(single["directory"])
    saved = directory.with_name("original-single")
    assert all(path.resolve().is_relative_to(tmp_path.resolve()) for path in (directory, saved))
    directory.rename(saved)
    subprocess.run(
        [
            "pwsh",
            "-NoProfile",
            "-Command",
            "New-Item -ItemType Junction -Path $env:B18_LINK -Target $env:B18_TARGET -ErrorAction Stop | Out-Null",
        ],
        env={**os.environ, "B18_LINK": str(directory), "B18_TARGET": permit["multi"]["directory"]},
        capture_output=True,
        check=True,
    )
    store = ExecutionStore(service.database)
    store.register()
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    result = service.task(task["task_id"])
    assert result["attempt"]["error"] == "b18_not_permitted"
    assert result["attempt"]["handed_off_at"] is None
    assert not (tmp_path / "data/executions" / attempt["id"] / "fixture-received.json").exists()


def test_ordinary_deployment_and_scoped_worker_are_isolated(setup, tmp_path, work_factory):
    service, permit = setup
    ordinary_config = replace(
        service.database.config,
        bilibili=replace(service.database.config.bilibili, b18_allowlist_file=None),
    )
    ordinary_db = db.Database(ordinary_config)
    try:
        normal = Service(ordinary_db)
        with pytest.raises(Problem) as error:
            register(normal, permit["single"])
        assert error.value.code == "UNSUPPORTED_OPTION"
        directory, manifest = work_factory()
        normal.registration(uid(), Registration(directory=str(directory), manifest=manifest))
        ordinary_id = normal.submit(manifest["work"]["id"], uid(), Submission())[1]["tasks"][0][
            "task_id"
        ]
        register(service, permit["single"])
        scoped_id = service.submit(
            permit["single"]["work_id"], uid(), Submission(retry_policy=ONCE)
        )[1]["tasks"][0]["task_id"]
        normal_worker = ExecutionStore(ordinary_db)
        scoped_worker = ExecutionStore(service.database)
        for worker, expected in ((scoped_worker, scoped_id), (normal_worker, ordinary_id)):
            worker.register()
            assert worker.claim()["task_id"] == expected
            assert worker.claim() is None
    finally:
        ordinary_db.close()


def test_uncertain_single_never_creates_a_second_submission(setup, tmp_path):
    service, permit = setup
    for work in (permit["single"], permit["multi"]):
        register(service, work)

    def submit(name):
        return service.submit(permit[name]["work_id"], uid(), Submission(retry_policy=ONCE))[1][
            "tasks"
        ][0]

    task_id = submit("single")["task_id"]
    write_json(tmp_path / "control.json", {"outcome": "uncertain", "query_failure": True})
    store = ExecutionStore(service.database)
    store.register()
    Supervisor(store).execute(store.claim())
    assert service.task(task_id)["status"] == "UNCERTAIN"
    assert submit("single")["task_id"] == task_id and store.claim() is None
    with pytest.raises(Problem, match="单 P"):
        submit("multi")
    assert len(list((tmp_path / "data/executions").glob("*/fixture-received.json"))) == 1


def test_query_recovering_single_needs_a_separate_readback(setup, tmp_path):
    service, permit = setup
    for work in (permit["single"], permit["multi"]):
        register(service, work)
    request = Submission(retry_policy=ONCE)
    single = service.submit(permit["single"]["work_id"], uid(), request)[1]["tasks"][0]["task_id"]
    multi = permit["multi"]["work_id"]
    write_json(tmp_path / "control.json", {"outcome": "uncertain", "submit_intent": True})
    store = ExecutionStore(service.database)
    store.register()
    Supervisor(store).execute(store.claim())
    recovered = service.task(single)
    assert recovered["status"] == "SUCCEEDED" and recovered["receipt"]["source"] == "bilibili-query"
    with pytest.raises(Problem, match="成功回查"):
        service.submit(multi, uid(), request)
    assert RemoteQueries(service).query(single, uid())[1]["result"]["aid"] == 123
    assert service.submit(multi, uid(), request)[1]["tasks"][0]["created"]


def test_worker_rechecks_single_query_before_multi_handoff(setup, tmp_path):
    service, permit = setup
    for work in (permit["single"], permit["multi"]):
        register(service, work)

    def submit(name):
        return service.submit(permit[name]["work_id"], uid(), Submission(retry_policy=ONCE))[1][
            "tasks"
        ][0]["task_id"]

    single = submit("single")
    store = ExecutionStore(service.database)
    store.register()
    Supervisor(store).execute(store.claim())
    queries = RemoteQueries(service)
    assert queries.query(single, uid())[1]["result"]["verified_mid"] == "42"
    multi = submit("multi")
    write_json(tmp_path / "control.json", {"query_failure": True})
    assert queries.query(single, uid())[1]["error"]
    attempt = store.claim()
    Supervisor(store).execute(attempt)
    assert service.task(single)["status"] == "SUCCEEDED"
    result = service.task(multi)
    assert result["status"] == "FAILED" and result["attempt"]["handed_off_at"] is None
    assert not (tmp_path / "data/executions" / attempt["id"] / "fixture-received.json").exists()


@pytest.fixture
def supplement(setup, tmp_path):
    service, permit = setup
    request = Submission(retry_policy=ONCE)
    store = ExecutionStore(service.database)
    store.register()

    def start(name):
        work = permit[name]
        register(service, work)
        task_id = service.submit(work["work_id"], uid(), request)[1]["tasks"][0]["task_id"]
        Supervisor(store).execute(store.claim())
        return service.task(task_id)

    single = start("single")
    RemoteQueries(service).query(single["id"], uid())
    write_json(
        tmp_path / "control.json", {"outcome": "retryable", "error_code": "VIDEO_UPLOAD_FAILED"}
    )
    old = start("multi")
    assert old["status"] == "FAILED" and old["attempt"]["exit_confirmed"]
    permit["supplement"] = {
        "single_task_id": single["id"],
        "failed_task_id": old["id"],
        "failed_attempt_id": old["attempt"]["id"],
    }
    write_json(tmp_path / "allowlist.json", permit)
    write_json(tmp_path / "control.json", {})
    return service, store, permit, single, old, request


def test_supplement_uses_old_query_and_reserves_only_one_successor(supplement):
    service, store, permit, single, old, request = supplement
    with pytest.raises(Problem, match="只开放原多 P"):
        register(service, permit["single"])
    with pytest.raises(Problem, match="只允许手动"):
        service.submit(old["work_id"], uid(), request)
    key = uid()

    def submit(request_id):
        try:
            return service.submit(old["work_id"], request_id, request, previous=old["id"])[1][
                "tasks"
            ][0]
        except Problem as exc:
            assert exc.code == "resubmit_conflict"
            return None

    keys = [key, key, uid(), uid()]
    with ThreadPoolExecutor(4) as pool:
        outcomes = list(pool.map(submit, keys))
    created = [task for task in outcomes if task]
    assert len({task["task_id"] for task in created}) == 1 and None in outcomes
    new_id = created[0]["task_id"]
    winning_key = next(k for k, task in zip(keys, outcomes, strict=True) if task)
    assert submit(winning_key)["task_id"] == new_id
    assert created[0]["submission_round"] == 2 and created[0]["previous_task_id"] == old["id"]
    Supervisor(store).execute(store.claim())
    new = service.task(new_id)
    assert new["status"] == "SUCCEEDED" and new["attempt_count"] == 1
    assert new["receipt"]["evidence"]["video_keys"] == ["fixture-key-0", "fixture-key-1"]
    assert service.task(old["id"]) == old and service.task(single["id"])["status"] == "SUCCEEDED"
    assert store.claim() is None


@pytest.mark.parametrize("change", ["wrong_attempt", "unknown", "receipt", "unconfirmed_exit"])
def test_supplement_denies_unsafe_original(supplement, tmp_path, change):
    service, store, permit, single, old, request = supplement
    if change == "wrong_attempt":
        permit["supplement"]["failed_attempt_id"] = uid()
        write_json(tmp_path / "allowlist.json", permit)
    else:
        with service.database.transaction() as conn:
            if change == "receipt":
                values = db.one(
                    conn, select(db.receipts).where(db.receipts.c.task_id == single["id"])
                )
                conn.execute(
                    insert(db.receipts).values(
                        **{
                            **values,
                            "id": uid(),
                            "task_id": old["id"],
                            "attempt_id": old["attempt"]["id"],
                        }
                    )
                )
            else:
                attempt = old["attempt"]
                patch = (
                    {"exit_confirmed": False}
                    if change == "unconfirmed_exit"
                    else {"result": {**attempt["result"], "submission_state": "unknown"}}
                )
                conn.execute(
                    update(db.attempts).where(db.attempts.c.id == attempt["id"]).values(**patch)
                )
    with pytest.raises(Problem) as error:
        service.submit(old["work_id"], uid(), request, previous=old["id"])
    assert error.value.code == "b18_not_permitted" and store.claim() is None


@pytest.mark.parametrize("revoke", ["allowlist", "single_query"])
def test_supplement_rechecks_admission_before_handoff(supplement, tmp_path, revoke):
    service, store, _, single, old, request = supplement
    new_id = service.submit(old["work_id"], uid(), request, previous=old["id"])[1]["tasks"][0][
        "task_id"
    ]

    def invalidate(point):
        if point != "after_identity":
            return
        if revoke == "allowlist":
            (tmp_path / "allowlist.json").rename(tmp_path / "withdrawn.json")
        else:
            write_json(tmp_path / "control.json", {"query_failure": True})
            assert RemoteQueries(service).query(single["id"], uid())[1]["error"]

    attempt = store.claim()
    Supervisor(store, fault=invalidate).execute(attempt)
    new = service.task(new_id)
    assert new["status"] == "FAILED" and new["attempt"]["handed_off_at"] is None
    assert not (tmp_path / "data/executions" / attempt["id"] / "fixture-received.json").exists()
