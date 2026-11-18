import json
from pathlib import Path
import sys

from conftest import save_manifest
from test_cli_server_worker import Environment, wait_until


def test_cli_account_login_upload_review_and_relogin(tmp_path, work_factory):
    environment = Environment(tmp_path)
    build = "core-protocol-v1-fixture"
    capability = tmp_path / "capabilities.json"
    capability.write_text(
        json.dumps(
            {
                "protocol_version": 1,
                "engine_build_id": build,
                "submit_api": "web",
                "upload_ready": True,
                "verified_publish_modes": ["public"],
                "metadata_limits": {
                    "title": 80,
                    "part_title": 80,
                    "description": 2000,
                    "tag": 20,
                    "tag_count": 10,
                    "part_count": 100,
                },
            }
        )
    )
    configuration = tmp_path / "engine.json"
    configuration.write_text(
        json.dumps(
            {
                "executable": sys.executable,
                "build_id": build,
                "state_root": str(tmp_path / "private"),
                "capabilities_file": str(capability),
                "allow_test_engine": True,
                "test_arguments": [str(Path(__file__).parents[1] / "helpers/bilibili_fixture.py")],
            }
        )
    )
    control = tmp_path / "control.json"
    control.write_text("{}")
    environment.env.update(
        UPLOAD_PLATFORM_BILIBILI_CONFIG=str(configuration), BUE_FIXTURE_CONTROL=str(control)
    )
    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        result = environment.cli("account", "login")["operation_result"]
        assert result["status"] == "SUCCEEDED", result
        account_id = result["account_id"]
        assert [
            a["remote_user_id"]
            for a in environment.cli("account", "list")["items"]
            if a["platform"] == "bilibili"
        ] == ["42"]
        directory, manifest = work_factory()
        for video in manifest["assets"]["videos"]:
            video.pop("subtitles", None)
        manifest["upload_units"][0]["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:42",
                "options": {"category_id": 171, "copyright": 1, "publish_mode": "public"},
            }
        ]
        save_manifest(directory, manifest)
        work_id = environment.cli("work", "register", "--dir", str(directory))["operation_result"][
            "work_id"
        ]
        task_id = environment.cli("work", "upload", work_id)["operation_result"]["tasks"][0][
            "task_id"
        ]
        environment.start("worker")
        wait_until(
            lambda: environment.http.get("/tasks/" + task_id).json()["status"] == "SUCCEEDED"
        )
        control.write_text(json.dumps({"review_state": -4}))
        query = environment.cli("task", "query", task_id)["operation_result"]
        assert query["result"]["review_status"] == -4
        environment.cli("account", "disable", account_id)
        assert (
            environment.cli("account", "login", "--account-id", account_id)["operation_result"][
                "status"
            ]
            == "SUCCEEDED"
        )
        account = next(
            a for a in environment.cli("account", "list")["items"] if a["id"] == account_id
        )
        assert not account["enabled"] and not account["blocks"]
        assert environment.cli("task", "status", task_id)["status"] == "SUCCEEDED"
    finally:
        environment.close()
