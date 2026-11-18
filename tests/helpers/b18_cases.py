import json
from pathlib import Path
import sys

from conftest import save_manifest
from upload_platform.domain.core import uid


def write_json(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def prepare_cases(tmp_path, work_factory):
    sample = Path(__file__).resolve().parents[2] / "docs/operations/b18-allowlist.example.json"
    allowlist = json.loads(sample.read_text(encoding="utf-8"))
    build = "core-protocol-v1-fixture"
    allowlist.update(run_id=uid(), core_data_dir=str(tmp_path / "data"), engine_build_id=build)
    for name in ("single", "multi"):
        approved = allowlist[name]
        directory, manifest = work_factory(directory=tmp_path / "works" / name)
        approved.update(directory=str(directory), work_id=manifest["work"]["id"])
        manifest["work"].update(title=approved["title"], description="合成测试片", tags=["测试"])
        unit = manifest["upload_units"][0]
        unit["metadata_overrides"] = {}
        unit["targets"] = [
            {
                "platform": "bilibili",
                "account": "bilibili:42",
                "options": {"category_id": 171, "copyright": 1, "publish_mode": "public"},
            }
        ]
        if name == "single":
            manifest["work"].pop("cover")
            unit.update(mode="independent", videos=["v1"])
        for part, video in zip(approved["parts"], manifest["assets"]["videos"]):
            video["metadata"] = {"title": part["title"]}
            video.pop("subtitles", None)
        save_manifest(directory, manifest)
    write_json(tmp_path / "allowlist.json", allowlist)
    write_json(
        tmp_path / "capabilities.json",
        {
            "protocol_version": 1,
            "engine_build_id": build,
            "submit_api": "web",
            "upload_ready": False,
            "verified_publish_modes": [],
            "metadata_limits": {
                "title": 80,
                "part_title": 80,
                "description": 2000,
                "tag": 20,
                "tag_count": 10,
                "part_count": 100,
            },
        },
    )
    write_json(
        tmp_path / "engine.json",
        {
            "executable": sys.executable,
            "build_id": build,
            "state_root": str(tmp_path / "private"),
            "capabilities_file": str(tmp_path / "capabilities.json"),
            "allow_test_engine": True,
            "test_arguments": [str(Path(__file__).with_name("bilibili_fixture.py"))],
            "b18_allowlist_file": str(tmp_path / "allowlist.json"),
        },
    )
    write_json(tmp_path / "control.json", {})
    return allowlist
