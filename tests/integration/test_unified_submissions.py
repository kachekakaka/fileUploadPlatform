from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys

from fastapi.testclient import TestClient
import pytest
from sqlalchemy import func, select
import yaml

from upload_platform.adapters.bilibili.config import EngineConfig
from upload_platform.api.app import create_app
from upload_platform.application.service import Service
from upload_platform.application.submissions import PREVIEW_TTL, version
from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db
from upload_platform.infrastructure.directory_picker import DirectoryPicker


SUBMIT = "/api/v1/submissions"
PREVIEW = SUBMIT + "/preview"
RESULT = SUBMIT + "/result"
ORIGIN = "http://127.0.0.1:8765"
OTHER_SITE = "http://127.0.0.1:8001"


@pytest.fixture
def setup(tmp_path, config, clock, work_factory):
    media, manifest = work_factory()
    config_root = tmp_path / "configuration"
    yaml_path = config_root / "sessions" / manifest["work"]["id"] / "upload.yaml"
    yaml_path.parent.mkdir(parents=True)
    for index, video in enumerate(manifest["assets"]["videos"], 1):
        previous = media / video["path"]
        video["path"] = f"job-{index}/video.mp4"
        dest = media / video["path"]
        dest.parent.mkdir()
        previous.rename(dest)
    yaml_path.write_bytes(yaml.safe_dump(manifest, allow_unicode=True).encode())
    database = db.Database(config)
    service = Service(database, clock)
    with TestClient(create_app(config, database, service), base_url=ORIGIN) as client:
        yield client, database, clock, service, yaml_path, media, manifest


def counts(database):
    with database.read() as conn:
        return [
            conn.execute(select(func.count()).select_from(table)).scalar_one()
            for table in (db.works, db.tasks, db.idempotency)
        ]


def paths(yaml_path, root):
    return {"manifest_path": str(yaml_path), "asset_root": str(root)}


def automatic(yaml_path, root):
    return {**paths(yaml_path, root), "expected_version": version(yaml_path.read_bytes())}


def headers(origin=None, key=None):
    return {
        **({"Origin": origin} if origin else {}),
        "Idempotency-Key": key or uid(),
    }


def test_preview_and_replay_after_yaml_removal_and_expiry(setup):
    client, database, clock, _, yaml_path, root, manifest = setup
    preflight = client.options(
        SUBMIT,
        headers={
            "Origin": OTHER_SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Content-Type, Idempotency-Key",
        },
    )
    assert preflight.status_code == 204
    assert preflight.headers["access-control-allow-origin"] == "*"
    assert "access-control-allow-credentials" not in preflight.headers
    assert counts(database) == [0, 0, 0]
    preview = client.post(PREVIEW, json=paths(yaml_path, root), headers=headers(OTHER_SITE))
    assert preview.status_code == 200, preview.text
    assert preview.json()["config_version"] == version(yaml_path.read_bytes())
    assert counts(database) == [0, 0, 0]
    request = {"preview_id": preview.json()["preview_id"], "source": {"application": "test"}}
    key = headers(OTHER_SITE)
    posted = client.post(SUBMIT, headers=key, json=request)
    assert posted.status_code == 201, posted.text
    result = posted.json()
    assert result["work_id"] == manifest["work"]["id"]
    assert len(result["task_ids"]) == 1
    task_id = result["task_ids"][0]
    snapshot = client.get("/api/v1/tasks/" + task_id).json()["snapshot"]
    assert snapshot["config_version"] == preview.json()["config_version"]
    assert Path(snapshot["manifest_path"]) == yaml_path
    assert Path(snapshot["directory"]) == root
    assert snapshot["manifest"] == preview.json()["manifest"]
    assert [p["relative_path"] for p in snapshot["assets"][:3:2]] == [
        "job-1/video.mp4",
        "job-2/video.mp4",
    ]
    yaml_path.rename(yaml_path.with_suffix(".missing"))
    clock.value += PREVIEW_TTL + 1
    assert client.get(RESULT, headers=key).json() == result
    assert client.post(SUBMIT, headers=key, json=request).json() == result
    conflict = client.post(SUBMIT, headers=key, json={**request, "source": None})
    assert conflict.status_code == 409 and conflict.json()["code"] == "idempotency_conflict"
    assert conflict.headers["access-control-allow-origin"] == "*"
    assert client.post(SUBMIT, headers=headers(), json=request).json()["code"] == "preview_expired"
    assert client.get(RESULT, headers=headers()).status_code == 404
    assert counts(database) == [1, 1, 1]


def test_first_submission_rejects_stale_and_invalid_yaml(setup):
    client, database, _, _, yaml_path, root, _ = setup
    body = automatic(yaml_path, root)
    preview = client.post(PREVIEW, json=paths(yaml_path, root)).json()
    original = yaml_path.read_bytes()
    yaml_path.write_bytes(original + b"\n# changed\n")
    for request in (body, {"preview_id": preview["preview_id"]}):
        response = client.post(SUBMIT, headers=headers(), json=request)
        assert response.status_code == 409 and response.json()["code"] == "version_conflict"
    for content in (
        b"\xff",
        b" " * (1024 * 1024 + 1),
        b"version: 1\nversion: 1\n",
        original + b"extra_field: true\n",
    ):
        yaml_path.write_bytes(content)
        error = client.post(PREVIEW, json=paths(yaml_path, root))
        assert error.status_code == 422 and error.json()["code"] == "invalid_manifest"
    assert counts(database) == [0, 0, 0]


def test_windows_junction_cannot_escape_requested_media_root(setup, tmp_path):
    client, database, _, _, yaml_path, root, manifest = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "upload.yaml").write_bytes(yaml_path.read_bytes())
    (outside / "video.mp4").write_bytes(b"outside")
    candidates = [
        (yaml_path.parent / "escape", "upload.yaml"),
        (root / "escape", "video.mp4"),
    ]
    for link, name in candidates:
        subprocess.run(
            [
                "pwsh",
                "-NoProfile",
                "-Command",
                "& {param($link,$target) "
                "New-Item -ItemType Junction -Path $link -Target $target | Out-Null}",
                str(link),
                str(outside),
            ],
            check=True,
        )
        assert (link / name).is_file()
    # The caller may name a separate local directory containing the manifest.
    response = client.post(PREVIEW, json=paths(candidates[0][0] / "upload.yaml", root))
    assert response.status_code == 200
    response = client.post(PREVIEW, json=paths(yaml_path, candidates[1][0]))
    assert response.status_code == 422 and response.json()["code"] == "invalid_path"
    manifest["assets"]["videos"][0]["path"] = "escape/video.mp4"
    yaml_path.write_bytes(yaml.safe_dump(manifest, allow_unicode=True).encode())
    response = client.post(SUBMIT, json=automatic(yaml_path, root), headers=headers())
    assert response.status_code == 422 and response.json()["code"] == "invalid_path"
    assert counts(database) == [0, 0, 0]


def test_missing_and_unreadable_submission_files(setup):
    import win32con
    import win32file

    client, database, _, _, yaml_path, root, manifest = setup
    body = automatic(yaml_path, root)
    for file in (yaml_path, root / manifest["assets"]["videos"][0]["path"]):
        moved = file.with_suffix(".missing")
        file.rename(moved)
        try:
            response = client.post(PREVIEW, json=paths(yaml_path, root))
            assert (response.status_code, response.json()["code"]) == (422, "invalid_path")
        finally:
            moved.rename(file)
        # A real Windows share lock denies reading to the process serving the request.
        handle = win32file.CreateFile(
            str(file), win32con.GENERIC_READ, 0, None, win32con.OPEN_EXISTING, 0, None
        )
        try:
            response = client.post(SUBMIT, json=body, headers=headers())
            assert (response.status_code, response.json()["code"]) == (422, "invalid_path")
        finally:
            handle.Close()
    assert counts(database) == [0, 0, 0]


def test_transaction_rollback_concurrency_and_shared_ledger(setup):
    client, database, _, service, yaml_path, root, manifest = setup
    body = automatic(yaml_path, root)
    key = headers()

    def fail_before_commit(point):
        if point == "before_commit":
            raise Problem("injected_failure", "rollback", 503)

    service.fault = fail_before_commit
    response = client.post(SUBMIT, json=body, headers=key)
    assert response.status_code == 503 and response.json()["code"] == "injected_failure"
    assert counts(database) == [0, 0, 0]
    service.fault = lambda _: None
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(client.post, SUBMIT, json=body, headers=key) for _ in range(4)]
    values = [future.result() for future in pending]
    assert all(value.status_code == 201 and value.json() == values[0].json() for value in values)
    assert counts(database) == [1, 1, 1]
    legacy = client.post(
        f"/api/v1/works/{manifest['work']['id']}/submit",
        json={"manifest": manifest, "directory": str(root)},
        headers={**headers(), "x-upload-platform-client": "cli-v1"},
    )
    assert legacy.status_code == 201, legacy.text
    assert legacy.json()["tasks"][0]["task_id"] == values[0].json()["task_ids"][0]
    assert not legacy.json()["tasks"][0]["created"]
    assert counts(database) == [1, 1, 2]


def test_independent_and_multipart_expand_across_enabled_targets(setup):
    client, database, _, _, yaml_path, root, manifest = setup
    with database.transaction() as conn:
        conn.execute(
            db.accounts.insert().values(
                id=uid(),
                platform="fake",
                reference="second-target",
                display_name="second",
                status="ACTIVE",
                concurrency_limit=1,
            )
        )
    unit = manifest["upload_units"][0]
    independent = {
        **unit,
        "id": "separate",
        "mode": "independent",
        "targets": [*unit["targets"], {**unit["targets"][0], "account": "second-target"}],
    }
    manifest["upload_units"].append(independent)
    yaml_path.write_bytes(yaml.safe_dump(manifest, allow_unicode=True).encode())
    result = client.post(SUBMIT, json=automatic(yaml_path, root), headers=headers())
    assert result.status_code == 201, result.text
    ids = result.json()["task_ids"]
    assert len(set(ids)) == 5
    for item in (
        {"platform": "fake", "since": "2026-11-16T00:00:00Z", "until": "2026-11-16T08:00:00+08:00"},
        {"work_id": manifest["work"]["id"], "status": "QUEUED", "sort": "oldest"},
    ):
        page = client.get("/api/v1/tasks", params=item).json()
        assert {row["id"] for row in page["items"]} == set(ids)
    assert (
        client.get("/api/v1/tasks", params={"since": "2026-11-17T00:00:00Z"}).json()["items"] == []
    )
    assert counts(database) == [1, 5, 1]


def test_web_entry_separates_posting_from_management(setup):
    client, database, _, _, yaml_path, root, _ = setup
    payload = automatic(yaml_path, root)
    for extra in ({"Host": "evil.example"}, {"X-Forwarded-Host": "127.0.0.1:8765"}):
        assert client.post(SUBMIT, json=payload, headers={**headers(), **extra}).status_code == 403
    assert (
        client.post(
            SUBMIT, content="{}", headers={**headers(), "Content-Type": "text/plain"}
        ).status_code
        == 400
    )
    assert counts(database) == [0, 0, 0]
    task_id = client.post(SUBMIT, json=payload, headers=headers()).json()["task_ids"][0]
    cross = {**headers(OTHER_SITE), "Sec-Fetch-Site": "same-site"}
    same = {**headers(ORIGIN), "Sec-Fetch-Site": "same-origin"}
    cancel = "/api/v1/tasks/" + task_id + "/cancel"
    session = "/api/v1/dashboard/session"
    assert client.get(session, headers=cross).status_code == 403
    assert client.post(cancel, headers=cross, json={}).status_code == 403
    assert client.post(cancel, headers=same, json={}).status_code == 403
    assert client.get("/api/v1/accounts", headers=same).status_code == 403
    token = client.get(session, headers=same)
    assert token.headers["cache-control"] == "no-store"
    auth = {"X-Upload-Platform-CSRF": token.json()["csrf_token"]}
    assert client.post(cancel, headers={**cross, **auth}, json={}).status_code == 403
    assert client.post(cancel, headers={**same, **auth}, json={}).status_code == 200
    assert client.get("/api/v1/tasks/" + task_id).json()["status"] == "CANCELLED"


def test_deployment_header_blocks_wrong_database_before_admission(setup):
    client, database, _, _, yaml_path, root, _ = setup
    header = "X-Upload-Platform-Deployment"
    health = client.get("/api/v1/system/health")
    deployment_id = health.json()["deployment_id"]
    assert health.headers[header] == deployment_id
    reopened = db.Database(database.config)
    try:
        assert reopened.initialize() == deployment_id
    finally:
        reopened.close()

    preflight = client.options(
        SUBMIT,
        headers={
            "Origin": OTHER_SITE,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": f"Content-Type, Idempotency-Key, {header}",
        },
    )
    assert preflight.status_code == 204
    assert header in preflight.headers["access-control-allow-headers"]

    wrong = {**headers(OTHER_SITE), header: uid()}
    for url in (PREVIEW, SUBMIT):
        reject = client.post(url, headers=wrong, json={})
        assert (reject.status_code, reject.json()["code"]) == (409, "binding")
        assert reject.headers[header] == deployment_id
        assert reject.headers["access-control-allow-origin"] == "*"
    assert counts(database) == [0, 0, 0]

    body = automatic(yaml_path, root)
    right = {**wrong, header: deployment_id}
    posted = client.post(SUBMIT, headers=right, json=body)
    assert posted.status_code == 201
    rejected_lookup = client.get(RESULT, headers=wrong)
    assert (rejected_lookup.status_code, rejected_lookup.json()["code"]) == (409, "binding")
    assert client.get(RESULT, headers=right).json() == posted.json()
    assert counts(database) == [1, 1, 1]


def test_picker_validates_selected_directory_before_returning(tmp_path, config, monkeypatch):
    allowed, outside = tmp_path / "allowed", tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    database = db.Database(config)
    with TestClient(create_app(config, database), base_url=ORIGIN) as client:
        browser = {**headers(ORIGIN), "Sec-Fetch-Site": "same-origin"}
        token = client.get("/api/v1/dashboard/session", headers=browser).json()["csrf_token"]
        browser["X-Upload-Platform-CSRF"] = token
        path = "/api/v1/dashboard/pick-directory"
        for selected, expected in (
            (None, 200),
            (str(allowed), 200),
            (str(outside), 200),
            (str(outside / "missing"), 422),
        ):
            monkeypatch.setattr(DirectoryPicker, "choose", lambda self, start: selected)
            response = client.post(path, headers=browser, json={})
            assert response.status_code == expected
            if expected == 200:
                assert response.json()["directory"] == selected
            else:
                assert response.json()["code"] == "invalid_path"
        assert counts(database) == [0, 0, 0]


def test_mumu_generated_single_and_session_yaml_contract(tmp_path, config):
    output = tmp_path / "output"
    sessions = output / "sessions"
    target_account = uid()
    for index in (1, 2):
        video = output / f"job-{index}" / "000001.mp4"
        video.parent.mkdir(parents=True)
        video.write_bytes(f"job {index}".encode())
    capabilities = tmp_path / "capabilities.json"
    capabilities.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "engine_build_id": "mumu-contract-fixture",
                "submit_api": "web",
                "upload_ready": True,
                "verified_publish_modes": ["self_only"],
                "supports_self_only": True,
                "metadata_limits": {
                    "title": 80,
                    "part_title": 80,
                    "description": 2000,
                    "tag": 20,
                    "tag_count": 10,
                    "part_count": 10,
                },
            }
        ),
        encoding="utf-8",
    )
    engine = EngineConfig(
        Path(sys.executable),
        "mumu-contract-fixture",
        tmp_path / "private",
        capabilities,
        allow_test_engine=True,
        allow_self_only=True,
    )
    sessions.mkdir()
    cfg = replace(config, bilibili=engine)
    database = db.Database(cfg)
    with TestClient(create_app(cfg, database), base_url=ORIGIN) as client:
        with database.transaction() as conn:
            conn.execute(
                db.accounts.insert().values(
                    id=target_account,
                    platform="bilibili",
                    reference="mumu-contract",
                    display_name="Synthetic Bilibili",
                    status="ACTIVE",
                    concurrency_limit=1,
                    remote_user_id="42",
                    credential_reference="local-fixture-unused",
                )
            )
        for mode, indexes, kind in (
            ("independent", [1], "manual"),
            ("multipart", [2, 1], "session"),
        ):
            work_id = uid()
            path = sessions / work_id / "upload.yaml"
            path.parent.mkdir()
            videos = [
                {"id": f"p{i}", "path": f"job-{i}/000001.mp4", "metadata": {"title": f"片段 {i}"}}
                for i in indexes
            ]
            manifest = {
                "version": 1,
                "work": {"id": work_id, "ready": True, "title": "合成场次", "tags": ["合成"]},
                "assets": {"videos": videos},
                "upload_units": [
                    {
                        "id": "main",
                        "mode": mode,
                        "videos": [video["id"] for video in videos],
                        "targets": [
                            {
                                "platform": "bilibili",
                                "account": target_account,
                                "options": {
                                    "category_id": 171,
                                    "copyright": 1,
                                    "publish_mode": "self_only",
                                },
                            }
                        ],
                    }
                ],
            }
            path.write_bytes(yaml.safe_dump(manifest, allow_unicode=True).encode())
            request = automatic(path, output)
            if kind == "manual":
                preview = client.post(PREVIEW, json=paths(path, output))
                assert preview.status_code == 200, preview.text
                request = {"preview_id": preview.json()["preview_id"]}
            request["source"] = {
                "application": "mumu",
                "kind": kind,
                "external_reference": "synthetic",
            }
            key = headers()
            posted = client.post(SUBMIT, json=request, headers=key)
            assert posted.status_code == 201, posted.text
            value = posted.json()
            assert value["work_id"] == work_id and len(value["task_ids"]) == 1
            snapshot = client.get("/api/v1/tasks/" + value["task_ids"][0]).json()["snapshot"]
            assert snapshot["mode"] == mode and snapshot["target"]["expected_mid"] == "42"
            assert snapshot["target"]["options"]["publish_mode"] == "self_only"
            assert snapshot["manifest"]["work"]["id"] == work_id
            actual_files = [
                Path(snapshot["directory"]) / item["relative_path"] for item in snapshot["assets"]
            ]
            assert [file.read_bytes() for file in actual_files] == [
                f"job {i}".encode() for i in indexes
            ]
            path.rename(path.with_suffix(".archived"))
            assert client.get(RESULT, headers=key).json() == value
            assert client.post(SUBMIT, json=request, headers=key).json() == value
        assert counts(database) == [2, 2, 2]
