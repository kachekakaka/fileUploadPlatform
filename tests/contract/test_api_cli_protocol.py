from copy import deepcopy
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from upload_platform.api.app import create_app
from upload_platform.cli.client import Client
from upload_platform.domain.core import uid
from upload_platform.execution.protocol import HostResult


@pytest.fixture
def api(config, database, service):
    with TestClient(
        create_app(config, database, service),
        base_url=config.api_url,
        raise_server_exceptions=False,
        headers={"X-Upload-Platform-Client": "cli-v1"},
    ) as client:
        yield client


def post(api, route, body, key=None):
    return api.post("/api/v1" + route, json=body, headers={"Idempotency-Key": key or uid()})


@pytest.mark.parametrize(
    "headers",
    [
        {"Origin": "null"},
        {"Origin": "https://evil.example"},
        {"Sec-Fetch-Site": "cross-site"},
        {"X-Forwarded-Host": "127.0.0.1:8765"},
        {"Forwarded": "for=127.0.0.1"},
        {"Host": "evil.example:8765"},
    ],
)
def test_browser_and_forwarding_rejected(api, headers):
    response = api.get("/api/v1/system/health", headers=headers)
    assert response.status_code == 403
    assert set(response.json()) == {"code", "message", "request_id", "details"}
    assert "access-control-allow-origin" not in response.headers


def test_missing_client_key_json_and_error_mapping(api):
    assert (
        api.post("/api/v1/works", json={}, headers={"X-Upload-Platform-Client": ""}).status_code
        == 403
    )
    assert (
        api.post(
            "/api/v1/works", content="body", headers={"Content-Type": "text/plain"}
        ).status_code
        == 400
    )
    assert (
        api.post(
            "/api/v1/works", content="{", headers={"Content-Type": "application/json"}
        ).status_code
        == 400
    )
    assert post(api, "/works", {}).status_code == 422
    assert api.get("/api/v1/tasks/" + uid()).status_code == 404


def test_api_work_submit_replay_preview_cancel_history(api, work_factory):
    directory, manifest = work_factory(independent=True)
    key = uid()
    registered = post(api, "/works", {"directory": str(directory), "manifest": manifest}, key)
    assert registered.status_code == 201
    work_id = registered.json()["work_id"]
    preview = post(api, f"/works/{work_id}/preview", {}).json()
    assert len(preview["expanded"]) == 2
    assert "account_id" not in preview["manifest"]["upload_units"][0]["targets"][0]
    body = {"manifest": preview["manifest"], "directory": preview["directory"]}
    key = uid()
    response = post(api, f"/works/{work_id}/submit", body, key)
    assert response.status_code == 201
    assert post(api, f"/works/{work_id}/submit", body, key).json() == response.json()
    changed = {**body, "retry_policy": {"max_attempts": 1, "retry_delays_seconds": []}}
    assert post(api, f"/works/{work_id}/submit", changed, key).status_code == 409
    task_id = response.json()["tasks"][0]["task_id"]
    assert api.get(f"/api/v1/tasks/{task_id}/attempts").json()["items"] == []
    cancelled = post(api, f"/tasks/{task_id}/cancel", {})
    assert cancelled.json()["status"] == "CANCELLED"
    assert api.get(f"/api/v1/tasks/{task_id}").json()["status"] == "CANCELLED"
    events = api.get(f"/api/v1/tasks/{task_id}/events").json()["items"]
    assert [e["sequence"] for e in events] == list(range(1, len(events) + 1))
    tail = api.get(f"/api/v1/tasks/{task_id}/events?after_sequence=1").json()["items"]
    assert tail == events[1:]
    result = post(api, f"/tasks/{task_id}/resubmit", {}).json()
    assert result["tasks"][0]["submission_round"] == 2


def test_stable_pagination_and_health_without_worker(api, submit_work):
    for _ in range(4):
        submit_work()
    first = api.get("/api/v1/tasks?limit=2").json()
    second = api.get("/api/v1/tasks?limit=2&cursor=" + first["next_cursor"]).json()
    ids = [t["id"] for t in first["items"] + second["items"]]
    assert len(set(ids)) == 4 and ids == sorted(ids)
    assert second["next_cursor"] is None
    assert api.get("/api/v1/tasks?limit=201").status_code == 422
    assert api.get("/api/v1/tasks?status=BOGUS").status_code == 422
    health = api.get("/api/v1/system/health")
    assert health.json() == {
        "api": "ready",
        "database": "ready",
        "workers": [],
        "deployment_id": health.headers["X-Upload-Platform-Deployment"],
    }


def test_schedule_api_versions_schema_and_idempotency(api, tmp_path):
    body = {"parent_directory": str(tmp_path), "kind": "daily", "local_time": "08:00:00"}
    key = uid()
    created = post(api, "/schedules", body, key)
    assert created.status_code == 201
    assert post(api, "/schedules", body, key).json() == created.json()
    identifier = created.json()["id"]
    route = f"/api/v1/schedules/{identifier}"
    patch_key = uid()
    patched = api.patch(
        route,
        json={"expected_version": 1, "enabled": False},
        headers={"Idempotency-Key": patch_key},
    )
    assert patched.status_code == 200 and patched.json()["version"] == 2
    assert (
        api.patch(
            route,
            json={"expected_version": 1, "enabled": False},
            headers={"Idempotency-Key": patch_key},
        ).json()
        == patched.json()
    )
    assert (
        api.patch(
            route, json={"expected_version": 1, "enabled": True}, headers={"Idempotency-Key": uid()}
        ).status_code
        == 409
    )
    assert (
        api.patch(
            route,
            json={"expected_version": 2, "timezone": "invalid-zone"},
            headers={"Idempotency-Key": uid()},
        ).status_code
        == 422
    )
    assert len(api.get(route).json()["versions"]) == 2
    assert api.get(route + "/runs").json()["items"] == []
    assert post(api, "/schedules", {**body, "weekday": 1}).status_code == 422


def test_patch_idempotency_preserves_omitted_versus_null(api, tmp_path):
    created = post(
        api,
        "/schedules",
        {
            "parent_directory": str(tmp_path),
            "kind": "weekly",
            "weekday": 1,
            "local_time": "08:00:00",
        },
    ).json()
    route = f"/api/v1/schedules/{created['id']}"
    key = uid()
    original = {"expected_version": 1, "local_time": "09:00:00"}
    headers = {"Idempotency-Key": key}
    saved = api.patch(route, json=original, headers=headers)
    assert saved.status_code == 200
    changed = api.patch(route, json={**original, "weekday": None}, headers=headers)
    assert changed.status_code == 409
    assert changed.json()["code"] == "idempotency_conflict"
    assert api.patch(route, json=original, headers=headers).json() == saved.json()
    assert len(api.get(route).json()["versions"]) == 2
    # Explicit null is meaningful when changing weekly to daily, and replays exactly.
    clear = {"expected_version": 2, "kind": "daily", "weekday": None}
    headers = {"Idempotency-Key": uid()}
    saved = api.patch(route, json=clear, headers=headers)
    assert saved.status_code == 200 and saved.json()["config"]["weekday"] is None
    assert api.patch(route, json=clear, headers=headers).json() == saved.json()
    assert (
        api.patch(route, json={"expected_version": 2, "kind": "daily"}, headers=headers).status_code
        == 409
    )


def test_patch_supplied_policy_still_normalizes_nested_defaults(api, tmp_path):
    created = post(
        api,
        "/schedules",
        {
            "parent_directory": str(tmp_path),
            "kind": "daily",
            "local_time": "08:00:00",
        },
    ).json()
    route = f"/api/v1/schedules/{created['id']}"
    headers = {"Idempotency-Key": uid()}
    original = {"expected_version": 1, "retry_policy": {}}
    saved = api.patch(route, json=original, headers=headers)
    assert saved.status_code == 200
    explicit = {
        "expected_version": 1,
        "retry_policy": {"max_attempts": 3, "retry_delays_seconds": [30, 120]},
    }
    assert api.patch(route, json=explicit, headers=headers).json() == saved.json()


def test_transaction_failure_returns_503_not_success(api, service, work_factory):
    directory, manifest = work_factory()

    def fault(point):
        if point == "before_commit":
            raise RuntimeError("private sensitive details")

    service.fault = fault
    response = post(api, "/works", {"directory": str(directory), "manifest": manifest})
    assert response.status_code == 503
    assert "private sensitive" not in response.text
    service.fault = lambda _: None
    assert api.get("/api/v1/works").json()["items"] == []


def test_client_log_before_network_and_resume(config, monkeypatch):
    client = Client(config)
    seen = []

    def transport(request):
        logs = list(config.request_logs.glob("*.json"))
        assert len(logs) == 1
        log = json.loads(logs[0].read_text(encoding="utf-8"))
        assert log["state"] == "pending"
        seen.append((request.headers["idempotency-key"], request.content))
        if len(seen) == 1:
            raise httpx.ReadError("response lost")
        return httpx.Response(201, json={"work_id": "original"})

    client.http.close()
    client.http = httpx.Client(
        base_url=config.api_url + "/api/v1",
        transport=httpx.MockTransport(transport),
        trust_env=False,
        follow_redirects=False,
    )
    from upload_platform.domain.core import Problem

    with pytest.raises(Problem) as failed:
        client.mutate("POST", "/works", {"description": "original body"})
    request_id = failed.value.details["request_id"]
    result = client.resume(request_id)
    assert result["operation_result"] == {"work_id": "original"}
    assert seen[0] == seen[1]
    assert (
        json.loads((config.request_logs / (request_id + ".json")).read_text(encoding="utf-8"))[
            "state"
        ]
        == "confirmed"
    )
    client.close()


def test_request_log_failure_never_sends(config, monkeypatch):
    from upload_platform.domain.core import Problem
    import upload_platform.cli.client as module

    client = Client(config)
    calls = []

    def failed(*_):
        raise OSError("disk failure")

    monkeypatch.setattr(module, "write_json", failed)
    monkeypatch.setattr(client.http, "request", lambda *a, **k: calls.append(a))
    with pytest.raises(Problem, match="未发送"):
        client.mutate("POST", "/works", {})
    assert calls == []
    client.close()


def test_protocol_rejects_forged_receipt_and_out_of_order_result():
    ident = {
        "protocol_version": 1,
        "task_id": uid(),
        "attempt_id": uid(),
        "execution_id": uid(),
        "generation": 1,
    }
    result = {
        **ident,
        "type": "result",
        "host_sequence": 3,
        "outcome": "succeeded",
        "safe": True,
        "fake": True,
        "receipt": {
            "fake": True,
            "execution_id": ident["execution_id"],
            "remote_id": "fake-" + ident["attempt_id"],
        },
        "history": [
            {**ident, "type": "hello", "host_sequence": 1},
            {**ident, "type": "progress", "host_sequence": 2},
        ],
    }
    HostResult.model_validate(result)
    for changed in [
        {**result, "protocol_version": 2},
        {**result, "receipt": {}},
        {**result, "host_sequence": 99},
        {**result, "history": list(reversed(result["history"]))},
    ]:
        with pytest.raises(ValidationError):
            HostResult.model_validate(changed)


def test_openapi_and_generated_schemas(config, work_factory):
    import jsonschema
    from upload_platform.application.contracts import Manifest

    api_schema = create_app(config).openapi()
    assert api_schema["openapi"].startswith("3.1")
    assert "/api/v1/tasks/{task_id}/resolve" in api_schema["paths"]
    headers = api_schema["paths"]["/api/v1/works"]["post"]["parameters"]
    assert any(header["name"] == "Idempotency-Key" and header["required"] for header in headers)
    assert api_schema["paths"]["/api/v1/works"]["post"]["responses"]["422"]["content"][
        "application/json"
    ]["schema"] == {"$ref": "#/components/schemas/ErrorResponse"}
    _, manifest = work_factory()
    jsonschema.Draft202012Validator.check_schema(Manifest.model_json_schema())
    jsonschema.validate(manifest, Manifest.model_json_schema())
    invalid = deepcopy(manifest)
    invalid["unknown"] = 1
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(invalid, Manifest.model_json_schema())
