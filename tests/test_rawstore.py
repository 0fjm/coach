import json

import pytest

from constants import NOW
from trainer.rawstore import is_done, mark_error, store_response


def test_store_response_upserts(db):
    assert store_response(db, "sleep", "2026-11-10", {"a": 1}, NOW) == "ok"
    store_response(db, "sleep", "2026-11-10", {"a": 2}, NOW)
    rows = db.execute("SELECT payload FROM raw_responses").fetchall()
    assert [json.loads(r[0]) for r in rows] == [{"a": 2}]


def test_store_response_empty_payload(db):
    assert store_response(db, "hrv", "2026-11-10", None, NOW) == "empty"
    assert is_done(db, "hrv", "2026-11-10")
    assert db.execute("SELECT COUNT(*) FROM raw_responses").fetchone()[0] == 0


def test_mark_error_counts_attempts_then_ok_clears(db):
    mark_error(db, "hrv", "d", "boom", NOW)
    mark_error(db, "hrv", "d", "boom", NOW)
    row = db.execute("SELECT status, attempts FROM fetch_status").fetchone()
    assert tuple(row) == ("error", 2)
    store_response(db, "hrv", "d", {"x": 1}, NOW)
    assert is_done(db, "hrv", "d")
