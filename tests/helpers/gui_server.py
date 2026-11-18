"""Dedicated real server used only by GUI login crash tests."""

import os
from pathlib import Path

import uvicorn
from sqlalchemy import select

from upload_platform.api.app import create_app
from upload_platform.config import Config
from upload_platform.infrastructure import database as db


app = create_app(Config.from_env())
crash_point = os.getenv("GUI_TEST_CRASH")
if crash_point:

    def fault(point):
        if point != crash_point:
            return
        with app.state.database.read() as conn:
            rows = db.many(conn, select(db.account_maintenance))
        if any(r["execution"].get("login_session", {}).get("phase") == "finalizing" for r in rows):
            Path(os.environ["GUI_TEST_MARKER"]).write_text("crashed at " + point)
            os._exit(83)

    app.state.service.fault = fault

uvicorn.run(
    app,
    host="127.0.0.1",
    port=int(os.environ["GUI_TEST_PORT"]),
    access_log=False,
    log_level="error",
)
