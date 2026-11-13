import copy
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from trainer.config import Settings
from trainer.db import connect
from trainer.derived import recompute_derived
from trainer.normalize import normalize
from trainer.queries import (
    QueryError,
    daily_status,
    describe_schema,
    list_activities,
    query_sql,
    training_load,
)
from trainer.rawstore import mark_status
from constants import DUPLICATE_ID

_FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def seeded_db_path(tmp_path):
    db_path = tmp_path / "t.db"
    conn = connect(db_path)

    items = json.loads((_FIXTURES / "activity_list.json").read_text())
    for item in items:
        normalize(conn, "activity_summary", str(item["activityId"]), item)

    base = next(i for i in items if i["activityId"] == 1001)
    item_1004 = copy.deepcopy(base)
    item_1004["activityId"] = 1004
    item_1004["activityType"] = {"typeKey": "cycling"}
    item_1004["startTimeLocal"] = "2026-11-09 08:03:00"
    item_1004["startTimeGMT"] = "2026-11-09 06:03:00"
    item_1004["duration"] = base["duration"] * 0.95
    for key in ("avgPower", "normPower", "maxPower"):
        item_1004.pop(key, None)
    normalize(conn, "activity_summary", "1004", item_1004)

    recompute_derived(conn)

    now = datetime(2026, 11, 10, 7, 0, tzinfo=UTC)
    user_summary_payload = json.loads((_FIXTURES / "user_summary.json").read_text())
    for d in ("2026-11-08", "2026-11-10"):
        normalize(conn, "user_summary", d, user_summary_payload)
        mark_status(conn, "user_summary", d, "ok", now)

    conn.execute(
        "INSERT INTO sync_runs (kind, started_at, finished_at, start_date, end_date, status, requests)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("sync", "2026-11-10T06:00:00", "2026-11-10T06:01:00", "2026-11-04", "2026-11-10", "ok", 10),
    )
    conn.commit()
    conn.close()

    return db_path


@pytest.mark.parametrize("sql", [
    "INSERT INTO ftp_history VALUES ('2026-01-01', 1)",
    "DELETE FROM activities",
    "DROP TABLE activities",
    "ATTACH DATABASE 'x.db' AS x",
    "PRAGMA writable_schema=ON",
    "SELECT 1; DELETE FROM activities",
])
def test_query_sql_rejects_non_reads(seeded_db_path, sql):
    with pytest.raises(QueryError):
        query_sql(seeded_db_path, sql, 10, Settings())
    assert not (seeded_db_path.parent / "x.db").exists()


def test_query_sql_caps_rows(seeded_db_path):
    r = query_sql(
        seeded_db_path,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n LIMIT 600) SELECT i FROM n",
        1000,
        Settings(),
    )
    assert r["row_count"] == 500 and r["truncated"] is True


def test_query_sql_times_out(seeded_db_path):
    started = time.monotonic()
    with pytest.raises(QueryError, match="timed out"):
        query_sql(
            seeded_db_path,
            "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n) SELECT count(*) FROM n",
            10,
            Settings(sql_timeout_s=0.5),
        )
    assert time.monotonic() - started < 3


def test_query_sql_includes_freshness(seeded_db_path):
    assert "last_sync_at" in query_sql(seeded_db_path, "SELECT 1", 10, Settings())["freshness"]


def test_daily_status_reports_missing_days(seeded_db_path):
    r = daily_status(seeded_db_path, "2026-11-08", "2026-11-10")
    assert r["missing_days"] == ["2026-11-09"]


def test_activities_excludes_duplicates_by_default(seeded_db_path):
    ids = {a["activity_id"] for a in list_activities(seeded_db_path, "2026-11-01", "2026-11-11")["rows"]}
    assert DUPLICATE_ID not in ids


def test_training_load_groups_by_iso_week(seeded_db_path):
    weeks = training_load(seeded_db_path, "2026-11-09", "2026-11-15")["weeks"]
    cycling = next(w for w in weeks if w["sport_family"] == "cycling")
    assert cycling["week"] == "2026-W46" and cycling["activities"] == 2


def test_describe_schema_lists_views_and_notes(seeded_db_path):
    s = describe_schema(seeded_db_path)
    assert {"v_activities", "v_daily_status"} <= set(s["views"]) and "duplicate_of" in s["notes"]


@pytest.mark.parametrize("max_rows", [-1, 0])
def test_query_sql_clamps_non_positive_max_rows(seeded_db_path, max_rows):
    r = query_sql(
        seeded_db_path,
        "WITH RECURSIVE n(i) AS (SELECT 1 UNION ALL SELECT i+1 FROM n LIMIT 600) SELECT i FROM n",
        max_rows,
        Settings(),
    )
    assert r["row_count"] == 1 and r["truncated"] is True
