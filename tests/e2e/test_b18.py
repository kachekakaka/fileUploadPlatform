import json

from helpers.b18_cases import prepare_cases, write_json
from test_cli_server_worker import Environment, wait_until


def test_two_scoped_posts_through_cli_server_and_worker(tmp_path, work_factory):
    approved = prepare_cases(tmp_path, work_factory)
    caps = tmp_path / "capabilities.json"
    original = caps.read_bytes()
    archive = tmp_path / "simulated-platform"
    archive.mkdir()
    environment = Environment(tmp_path)
    environment.env.update(
        UPLOAD_PLATFORM_BILIBILI_CONFIG=str(tmp_path / "engine.json"),
        BUE_FIXTURE_CONTROL=str(tmp_path / "control.json"),
    )

    def submit(work, *, success=True):
        return environment.cli(
            "work", "upload", work["work_id"], "--max-attempts", "1", success=success
        )

    try:
        environment.start("server")
        wait_until(lambda: environment.http.get("/system/health").status_code == 200)
        assert environment.cli("account", "login")["operation_result"]["status"] == "SUCCEEDED"
        for work in (approved["single"], approved["multi"]):
            environment.cli("work", "register", "--dir", work["directory"])
            preview = environment.cli("work", "preview", work["work_id"])["expanded"]
            assert [p["title"] for p in preview[0]["parts"]] == [p["title"] for p in work["parts"]]
        denied = submit(approved["multi"], success=False)
        assert denied.returncode and "b18_not_permitted" in denied.stderr
        environment.start("worker")
        ids = []
        for name, aid, bvid in (("single", 123, "BV1test00001"), ("multi", 456, "BV1test00002")):
            work = approved[name]
            write_json(
                tmp_path / "control.json", {"aid": aid, "bvid": bvid, "archive": str(archive)}
            )
            task_id = submit(work)["operation_result"]["tasks"][0]["task_id"]
            ids.append(task_id)
            wait_until(
                lambda: environment.http.get("/tasks/" + task_id).json()["status"] == "SUCCEEDED"
            )
            task = environment.cli("task", "status", task_id)
            assert task["attempt_count"] == 1 and task["attempt"]["exit_confirmed"]
            assert task["receipt"]["evidence"]["aid"] == aid
            if name == "single":
                denied = submit(approved["multi"], success=False)
                assert denied.returncode and "b18_not_permitted" in denied.stderr
                write_json(
                    tmp_path / "control.json", {"query_failure": True, "archive": str(archive)}
                )
                assert environment.cli("task", "query", task_id)["operation_result"]["error"]
                denied = submit(approved["multi"], success=False)
                assert denied.returncode and "b18_not_permitted" in denied.stderr
                write_json(tmp_path / "control.json", {"archive": str(archive)})
            query = environment.cli("task", "query", task_id)["operation_result"]["result"]
            assert (query["verified_mid"], query["aid"], query["bvid"]) == ("42", aid, bvid)
            reused = submit(work)["operation_result"]["tasks"][0]
            assert reused["task_id"] == task_id and not reused["created"]
            received = (
                tmp_path / "data/executions" / task["attempt"]["id"] / "fixture-received.json"
            )
            packet = json.loads(received.read_text())["request"]
            assert packet["identity"]["expected_mid"] == "42"
            assert packet["parts"] == [
                {"path": p["path"], "title": p["title"]} for p in work["parts"]
            ]
            assert packet["title"] == work["title"] and packet["cover"] == work["cover"]
            assert packet["options"] == work["options"]
            stored = json.loads((archive / f"{aid}.json").read_text(encoding="utf-8"))
            assert (stored["title"], stored["parts"]) == (work["title"], packet["parts"])
            assert query["video_keys"] == stored["video_keys"]
        assert ids[0] != ids[1]
        assert sorted(p.name for p in archive.glob("*.json")) == ["123.json", "456.json"]
        assert len(list((tmp_path / "data/executions").glob("*/fixture-received.json"))) == 2
        assert caps.read_bytes() == original
    finally:
        environment.close()
