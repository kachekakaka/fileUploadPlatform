from pathlib import Path
import sqlite3

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from upload_platform.domain.core import Problem, uid
from upload_platform.infrastructure import database as db


def test_actual_v1_database_rejected_before_write_transaction_or_wal(config):
    config.data_dir.mkdir(parents=True)
    with sqlite3.connect(config.database) as conn:
        conn.executescript(
            (Path(__file__).parents[1] / "fixtures/schema-v1.sql").read_text(encoding="utf-8")
        )
        conn.execute("INSERT INTO schema_version VALUES ('core',1)")
        conn.execute(
            "INSERT INTO accounts VALUES ('old-account','fake','fake-default','Original account','ACTIVE',1)"
        )
    before = config.database.read_bytes()
    database = db.Database(config)
    database.transaction = lambda: pytest.fail("must reject an old database before the write path")
    try:
        with pytest.raises(Problem) as exc:
            database.initialize()
        assert exc.value.code == "unsupported_database"
    finally:
        database.close()
    assert config.database.read_bytes() == before
    assert not Path(str(config.database) + "-wal").exists()
    with sqlite3.connect(config.database) as conn:
        assert conn.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert conn.execute("SELECT display_name FROM accounts").fetchone() == ("Original account",)


def test_new_database_identity_unique_and_concurrency_bounded(database):
    account = dict(
        platform="bilibili",
        reference="bilibili:42",
        display_name="first",
        status="ACTIVE",
        remote_user_id="42",
        credential_reference="42-version.json",
        concurrency_limit=3,
    )
    with database.transaction() as conn:
        conn.execute(insert(db.accounts).values(id=uid(), **account))
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(), **{**account, "reference": "another", "display_name": "renamed"}
            )
        )
    for invalid in ["0", "042", "4x", ""]:
        with pytest.raises(IntegrityError), database.transaction() as conn:
            conn.execute(
                insert(db.accounts).values(
                    id=uid(), **{**account, "reference": uid(), "remote_user_id": invalid}
                )
            )
    with pytest.raises(IntegrityError), database.transaction() as conn:
        conn.execute(
            insert(db.accounts).values(
                id=uid(),
                **{
                    **account,
                    "reference": "bilibili:99",
                    "remote_user_id": "99",
                    "concurrency_limit": 4,
                },
            )
        )
    database.initialize()
    with database.read() as conn:
        assert conn.execute(select(db.schema_version.c.version)).scalar_one() == db.SCHEMA_VERSION
        stored = db.one(conn, select(db.accounts).where(db.accounts.c.remote_user_id == "42"))
        assert stored["enabled"] and stored["version"] == 1
