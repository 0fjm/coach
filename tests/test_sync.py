from __future__ import annotations

from datetime import date, timedelta

import pytest

from constants import NOW, TODAY
from fakes import FakeSource
from trainer.config import Settings
from trainer.garmin_source import AuthRequired, FetchFailed, RateLimited
import trainer.sync as sync_module
from trainer.rawstore import mark_status
from trainer.sync import (
    SyncBusy,
    last_sync_at,
    run_backfill,
    run_sync,
    sync_lock,
    sync_window,
)


def exploding_factory():
    raise AssertionError("source_factory should not be called")


@pytest.fixture
def lock(tmp_path):
    return tmp_path / "sync.lock"


@pytest.fixture
def recent_sync(db):
    mark_status(db, "user_summary", (TODAY - timedelta(days=1)).isoformat(), "ok", NOW)


@pytest.fixture
def synced_once(db, lock, recent_sync):
    src = FakeSource.default()
    run_sync(db, lambda: src, lock, TODAY, NOW, Settings())
    return src


def test_window_is_last_synced_day_minus_overlap(db):
    mark_status(db, "user_summary", "2026-11-08", "ok", NOW)
    assert sync_window(db, date(2026, 11, 11), Settings()) == (date(2026, 11, 1), date(2026, 11, 11))


def test_large_gap_does_not_touch_garmin(db, lock):
    mark_status(db, "user_summary", "2026-10-01", "ok", NOW)
    r = run_sync(db, exploding_factory, lock, date(2026, 11, 11), NOW, Settings())
    assert r.status == "gap_too_large" and "trainer backfill" in r.message


def test_never_synced_points_to_backfill(db, lock):
    assert "trainer backfill" in run_sync(db, exploding_factory, lock, TODAY, NOW, Settings()).message


def test_sync_is_idempotent(db, lock, synced_once):
    counts = lambda: db.execute(
        "SELECT (SELECT COUNT(*) FROM raw_responses), (SELECT COUNT(*) FROM activities)"
    ).fetchone()
    before = tuple(counts())
    run_sync(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings())
    assert tuple(counts()) == before


def test_activity_details_fetched_once_and_only_when_applicable(db, lock, synced_once):
    src = FakeSource.default()
    run_sync(db, lambda: src, lock, TODAY, NOW, Settings())
    assert not [c for c in src.calls if c[0] == "activity"]
    first = synced_once.calls
    assert ("activity", "activity_power_zones", 1002) in first
    assert ("activity", "activity_power_zones", 1003) not in first
    assert ("activity", "activity_exercise_sets", 1003) in first


def test_auth_required_stops_and_reports(db, lock, recent_sync):
    src = FakeSource.default(
        raise_on={"list_activities": AuthRequired("Garmin session expired. Run `trainer login` in a terminal.")}
    )
    r = run_sync(db, lambda: src, lock, TODAY, NOW, Settings())
    assert r.status == "auth_required" and "trainer login" in r.message


def test_rate_limited_keeps_partial_data(db, lock, recent_sync):
    src = FakeSource.default(raise_on={("daily", "hrv"): RateLimited("stop")})
    r = run_sync(db, lambda: src, lock, TODAY, NOW, Settings())
    assert r.status == "rate_limited"
    assert db.execute("SELECT COUNT(*) FROM activities").fetchone()[0] > 0


def test_sync_returns_busy_while_locked(db, lock, recent_sync):
    with sync_lock(lock):
        r = run_sync(db, exploding_factory, lock, TODAY, NOW, Settings())
    assert r.status == "busy"


def test_backfill_goes_newest_first_and_resumes(db, lock):
    src = FakeSource.default()
    run_backfill(db, lambda: src, lock, TODAY, NOW, Settings(), since=date(2026, 9, 1))
    days = [c[2] for c in src.calls if c[:2] == ("daily", "user_summary")]
    assert days[0] == TODAY and days == sorted(days, reverse=True)
    again = FakeSource.default()
    run_backfill(db, lambda: again, lock, TODAY, NOW, Settings(), since=date(2026, 9, 1))
    assert len([c for c in again.calls if c[0] == "daily"]) < len(days)


def test_backfill_defaults_to_last_365_days(db, lock):
    src = FakeSource(days={}, activities=[], raise_on={})
    run_backfill(db, lambda: src, lock, TODAY, NOW, Settings())
    days = [c[2] for c in src.calls if c[:2] == ("daily", "user_summary")]
    assert min(days) == TODAY - timedelta(days=365)
    assert min(c[1] for c in src.calls if c[0] == "list_activities") >= TODAY - timedelta(days=365)


def test_fetch_failed_on_list_activities_continues(db, lock, recent_sync):
    src = FakeSource.default(raise_on={"list_activities": FetchFailed("network error")})
    r = run_sync(db, lambda: src, lock, TODAY, NOW, Settings())
    assert r.status == "ok"
    assert db.execute("SELECT COUNT(*) FROM daily_metrics").fetchone()[0] > 0


def test_factory_auth_required_no_stuck_row_sync(db, lock, recent_sync):
    def auth_factory():
        raise AuthRequired("Garmin session expired. Run `trainer login` in a terminal.")

    r = run_sync(db, auth_factory, lock, TODAY, NOW, Settings())
    assert r.status == "auth_required"
    assert "trainer login" in r.message
    rows = db.execute("SELECT status FROM sync_runs").fetchall()
    assert not any(row[0] == "running" for row in rows)


def test_factory_auth_required_no_stuck_row_backfill(db, lock):
    def auth_factory():
        raise AuthRequired("Garmin session expired. Run `trainer login` in a terminal.")

    r = run_backfill(db, auth_factory, lock, TODAY, NOW, Settings())
    assert r.status == "auth_required"
    assert "trainer login" in r.message
    rows = db.execute("SELECT status FROM sync_runs").fetchall()
    assert not any(row[0] == "running" for row in rows)


def _crashing_factory():
    raise RuntimeError("boom secret detail")


def test_crashed_sync_is_recorded_as_error(db, lock, synced_once):
    before = last_sync_at(db)
    later = NOW + timedelta(hours=1)
    r = run_sync(db, _crashing_factory, lock, TODAY, later, Settings())
    assert r.status == "error"
    assert r.message == "Sync failed: RuntimeError. Check the terminal with `trainer sync`."
    assert db.execute("SELECT status FROM sync_runs ORDER BY id DESC LIMIT 1").fetchone()[0] == "error"
    assert last_sync_at(db) == before


def test_crashed_backfill_is_recorded_as_error(db, lock):
    r = run_backfill(db, _crashing_factory, lock, TODAY, NOW, Settings())
    assert r.status == "error" and "RuntimeError" in r.message and "boom" not in r.message
    assert db.execute("SELECT status FROM sync_runs").fetchone()[0] == "error"
    assert last_sync_at(db) is None


def test_normalize_failure_is_recorded_and_sync_continues(db, lock, recent_sync, monkeypatch):
    real_normalize = sync_module.normalize

    def flaky(conn, endpoint, key, payload):
        if endpoint == "hrv":
            raise ValueError("bad shape")
        real_normalize(conn, endpoint, key, payload)

    monkeypatch.setattr(sync_module, "normalize", flaky)
    r = run_sync(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings())
    assert r.status == "ok"
    key = TODAY.isoformat()
    row = db.execute("SELECT status, last_error FROM fetch_status WHERE endpoint='hrv' AND key=?", (key,)).fetchone()
    assert tuple(row) == ("error", "normalize: ValueError")
    assert db.execute("SELECT COUNT(*) FROM raw_responses WHERE endpoint='hrv' AND key=?", (key,)).fetchone()[0] == 1
    assert db.execute("SELECT resting_hr FROM daily_metrics WHERE date=?", (key,)).fetchone()[0] is not None


def test_backfill_relists_partially_listed_month(db, lock):
    run_backfill(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings(), since=date(2026, 9, 15))
    earlier = FakeSource.default()
    run_backfill(db, lambda: earlier, lock, TODAY, NOW, Settings(), since=date(2026, 8, 1))
    assert ("list_activities", date(2026, 9, 1), date(2026, 9, 30)) in earlier.calls
    again = FakeSource.default()
    run_backfill(db, lambda: again, lock, TODAY, NOW, Settings(), since=date(2026, 8, 1))
    listed = [c[1] for c in again.calls if c[0] == "list_activities"]
    assert date(2026, 9, 1) not in listed and date(2026, 8, 1) not in listed


def test_sync_result_reports_freshness_after_the_run(db, lock, recent_sync):
    r = run_sync(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings())
    assert r.last_sync_at == NOW.isoformat() and r.last_synced_day == TODAY.isoformat()


def test_backfill_result_reports_freshness_after_the_run(db, lock):
    r = run_backfill(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings(), since=date(2026, 11, 1))
    assert r.last_sync_at == NOW.isoformat() and r.last_synced_day == TODAY.isoformat()


def test_ok_messages_say_what_was_fetched(db, lock, recent_sync):
    r = run_sync(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings())
    n = sum(r.fetched.values())
    assert r.message == f"Synced {r.start} to {r.end}: {n} responses, 3 new activities."
    b = run_backfill(db, lambda: FakeSource.default(), lock, TODAY, NOW, Settings(), since=date(2026, 11, 1))
    assert b.message == f"Backfilled 2026-11-01 to {TODAY}: {sum(b.fetched.values())} responses, 0 new activities."
