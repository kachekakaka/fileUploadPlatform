from copy import deepcopy
from pathlib import Path
import os

import pytest
import yaml

from upload_platform.application.contracts import Registration, Submission
from upload_platform.application.service import Service
from upload_platform.config import Config
from upload_platform.domain.core import uid, timestamp
from upload_platform.infrastructure.database import Database


def pytest_configure(config):
    if not config.option.basetemp:
        parent = Path(
            os.getenv(
                "UPLOAD_PLATFORM_TEST_ROOT",
                str(
                    Path(__file__).resolve().parents[2]
                    / "fileUploadPlatform_tmp"
                    / "core-mvp-tests"
                ),
            )
        )
        parent.mkdir(parents=True, exist_ok=True)
        config.option.basetemp = str(parent / uid())


class Clock:
    def __init__(self):
        self.value = timestamp("2026-11-16T00:00:00Z")

    def __call__(self):
        return self.value


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def config(tmp_path):
    return Config(
        tmp_path / "runtime",
        heartbeat_seconds=0.3,
        lease_seconds=2,
        handshake_timeout=4,
        cancel_timeout=1,
        execution_timeout=8,
        poll_seconds=0.05,
    )


@pytest.fixture
def database(config):
    database = Database(config)
    database.initialize()
    yield database
    database.close()


@pytest.fixture
def service(database, clock):
    return Service(database, clock)


@pytest.fixture
def work_factory(tmp_path):
    counter = 0

    def make(
        *,
        directory=None,
        scenario="success",
        duration=0.05,
        ready=True,
        work_id=None,
        independent=False,
    ):
        nonlocal counter
        counter += 1
        directory = Path(directory) if directory else tmp_path / f"work-{counter}"
        directory.mkdir(parents=True)
        for file in ("first.mp4", "second.mp4", "cover.jpg", "zh.srt"):
            (directory / file).write_bytes(b"synthetic-fixture")
        manifest = {
            "version": 1,
            "work": {
                "id": work_id or uid(),
                "ready": ready,
                "title": "作品标题",
                "description": "简介",
                "tags": ["作品"],
                "cover": "cover",
            },
            "assets": {
                "videos": [
                    {
                        "id": "v1",
                        "path": "first.mp4",
                        "subtitles": ["zh"],
                        "metadata": {"title": "第一 P", "description": "独立简介"},
                    },
                    {"id": "v2", "path": "second.mp4", "metadata": {"title": "第二 P"}},
                ],
                "covers": [{"id": "cover", "path": "cover.jpg"}],
                "subtitles": [{"id": "zh", "path": "zh.srt", "language": "zh-CN"}],
            },
            "upload_units": [
                {
                    "id": "main",
                    "mode": "independent" if independent else "multipart",
                    "videos": ["v1", "v2"],
                    "metadata_overrides": {"tags": ["单元"]},
                    "targets": [
                        {
                            "platform": "fake",
                            "account": "fake-default",
                            "options": {"scenario": scenario, "duration_seconds": duration},
                        }
                    ],
                }
            ],
        }
        save_manifest(directory, manifest)
        return directory, manifest

    return make


def save_manifest(directory, manifest):
    (directory / "upload.yaml").write_text(
        yaml.safe_dump(manifest, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )


@pytest.fixture
def submit_work(service, work_factory):
    def submit(**options):
        directory, manifest = work_factory(**options)
        work_id = manifest["work"]["id"]
        service.registration(
            uid(), Registration(directory=str(directory), manifest=deepcopy(manifest))
        )
        _, response = service.submit(work_id, uid(), Submission())
        return directory, manifest, response["tasks"]

    return submit
