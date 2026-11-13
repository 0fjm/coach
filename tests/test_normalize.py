import json
from pathlib import Path

import pytest

from constants import NOW
from trainer.normalize import normalize, rebuild, sport_family
from trainer.rawstore import store_response


FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.mark.parametrize("endpoint, column, expected", [
    ("user_summary", "resting_hr", 46),
    ("sleep", "sleep_score", 81),
    ("hrv", "hrv_status", "BALANCED"),
    ("training_readiness", "readiness_morning_score", 72),
    ("training_status", "acwr", 1.1),
    ("body_composition", "weight_kg", 72.4),
])
def test_daily_endpoint_maps_column(db, endpoint, column, expected):
    normalize(db, endpoint, "2026-11-10", load_fixture(endpoint))
    assert db.execute(f"SELECT {column} FROM daily_metrics WHERE date='2026-11-10'").fetchone()[0] == expected


def test_daily_endpoints_merge_into_one_row(db):
    normalize(db, "sleep", "2026-11-10", load_fixture("sleep"))
    normalize(db, "hrv", "2026-11-10", load_fixture("hrv"))
    row = db.execute("SELECT sleep_score, hrv_status FROM daily_metrics").fetchall()
    assert [tuple(r) for r in row] == [(81, "BALANCED")]


def test_missing_fields_become_null(db):
    normalize(db, "sleep", "2026-11-10", {"dailySleepDTO": {}})
    assert db.execute("SELECT sleep_s FROM daily_metrics").fetchone()[0] is None


def test_activity_summary_and_strength_sets(db):
    for item in load_fixture("activity_list"):
        normalize(db, "activity_summary", str(item["activityId"]), item)
    normalize(db, "activity_exercise_sets", "1003", load_fixture("activity_exercise_sets"))
    assert db.execute("SELECT sport_family FROM activities WHERE activity_id=1002").fetchone()[0] == "cycling"
    assert db.execute("SELECT weight_kg FROM exercise_sets WHERE activity_id=1003 AND set_index=0").fetchone()[0] == 20.0


def test_sport_family():
    assert [sport_family(k) for k in ("virtual_ride", "indoor_cycling", "road_biking", "trail_running", "strength_training", "hiking")] == \
        ["cycling", "cycling", "cycling", "running", "strength", "hiking"]


def test_rebuild_reproduces_tables(db):
    for endpoint in ("user_summary", "sleep", "hrv"):
        store_response(db, endpoint, "2026-11-10", load_fixture(endpoint), NOW)
        normalize(db, endpoint, "2026-11-10", load_fixture(endpoint))
    before = db.execute("SELECT * FROM daily_metrics").fetchall()
    assert rebuild(db) == 3
    assert db.execute("SELECT * FROM daily_metrics").fetchall() == before


def test_training_status_accepts_device_keyed_dicts_and_lists(db):
    payload = load_fixture("training_status")
    status = payload["mostRecentTrainingStatus"]
    status["latestTrainingStatusData"] = {"1234567": status["latestTrainingStatusData"][0]}
    balance = payload["mostRecentTrainingLoadBalance"]
    balance["metricsTrainingLoadBalanceDTOMap"] = list(balance["metricsTrainingLoadBalanceDTOMap"].values())
    normalize(db, "training_status", "2026-11-10", payload)
    row = db.execute("SELECT acwr, training_status, load_aerobic_low FROM daily_metrics").fetchone()
    assert tuple(row) == (1.1, "TRAINING_STATUS_MAINTAINING", 100.0)


def test_ftp_range_list_fills_history(db):
    normalize(db, "ftp_range", "2026-11", load_fixture("ftp_range"))
    rows = db.execute("SELECT date, ftp_w FROM ftp_history ORDER BY date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-01", 250), ("2026-11-01", 265)]


def test_ftp_range_dict_holding_list_skips_incomplete_entries(db):
    payload = {"entries": load_fixture("ftp_range") + [{"value": 300}, {"calendarDate": "2026-11-05"}]}
    normalize(db, "ftp_range", "2026-11", payload)
    rows = db.execute("SELECT date, ftp_w FROM ftp_history ORDER BY date").fetchall()
    assert [tuple(r) for r in rows] == [("2026-10-01", 250), ("2026-11-01", 265)]


def test_activity_splits_fill_laps(db):
    normalize(db, "activity_splits", "1002", load_fixture("activity_splits"))
    row = db.execute("SELECT lap_index, duration_s, avg_power_w, norm_power_w FROM activity_laps WHERE activity_id=1002").fetchall()
    assert [tuple(r) for r in row] == [(0, 3600.0, 180.0, 185.0)]


@pytest.mark.parametrize("endpoint, kind", [("activity_hr_zones", "hr"), ("activity_power_zones", "power")])
def test_activity_zones_fill_by_kind(db, endpoint, kind):
    normalize(db, endpoint, "1002", load_fixture(endpoint))
    rows = db.execute("SELECT zone, seconds FROM activity_zones WHERE activity_id=1002 AND kind=?", (kind,)).fetchall()
    assert len(rows) == 5 and tuple(rows[2]) == (3, 1800.0)
