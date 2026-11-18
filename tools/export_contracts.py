"""Regenerate public schemas from the actual Python contracts; no business DB opened."""

import json
import os
from pathlib import Path

from upload_platform.api.app import create_app
from upload_platform.application.contracts import Manifest, FakeOptions, BilibiliOptions
from upload_platform.adapters.bilibili.protocol import Message, Result
from upload_platform.application.login_sessions import (
    LoginEvent,
    LoginRequest,
    LoginView,
    AccountSummary,
)
from upload_platform.config import Config
from upload_platform.execution.protocol import HostRequest, HostEvent, HostResult, Command


def main():
    destination = Path(__file__).resolve().parents[1] / "schemas"
    destination.mkdir(exist_ok=True)
    config = Config(Path(os.environ["UPLOAD_PLATFORM_DATA_DIR"]))
    assets = {"openapi.json": create_app(config).openapi()}
    for name, model in [
        ("manifest-v1", Manifest),
        ("fake-options-v1", FakeOptions),
        ("bilibili-options-v1", BilibiliOptions),
        ("bilibili-event-v1", Message),
        ("bilibili-result-v1", Result),
        ("login-event-v1", LoginEvent),
        ("login-request-v1", LoginRequest),
        ("login-session-v1", LoginView),
        ("account-summary-v1", AccountSummary),
        ("host-request-v1", HostRequest),
        ("host-event-v1", HostEvent),
        ("host-result-v1", HostResult),
        ("host-command-v1", Command),
    ]:
        assets[name + ".json"] = model.model_json_schema()
    for name, body in assets.items():
        (destination / name).write_text(
            json.dumps(body, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


if __name__ == "__main__":
    main()
