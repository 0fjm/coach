import sqlite3

import pytest

from trainer.db import MIGRATIONS, connect, connect_ro, migrate


def test_migrate_is_idempotent(tmp_path):
    conn = connect(tmp_path / "t.db")
    migrate(conn)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)


def test_connect_ro_rejects_writes(tmp_path):
    connect(tmp_path / "t.db").close()
    ro = connect_ro(tmp_path / "t.db")
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO ftp_history VALUES ('2026-01-01', 250)")
