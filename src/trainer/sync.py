from __future__ import annotations

import calendar
import fcntl
import sqlite3
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Literal

from trainer.config import Settings
from trainer.derived import recompute_derived
from trainer.garmin_source import DAILY_ENDPOINTS, AuthRequired, FetchFailed, RateLimited, Source
from trainer.normalize import normalize
from trainer.rawstore import is_done, mark_error, mark_status, store_response


SyncStatus = Literal["ok", "busy", "gap_too_large", "auth_required", "rate_limited", "error"]


class SyncBusy(Exception):
    pass


@contextmanager
def sync_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = open(lock_path, "w")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        fd.close()
        raise SyncBusy("A backfill or sync is already running. Try again when it finishes.")
    try:
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        fd.close()


@dataclass
class SyncResult:
    status: SyncStatus
    start: date | None
    end: date | None
    requests: int
    fetched: dict[str, int]
    new_activities: int
    last_sync_at: str | None
    last_synced_day: str | None
    message: str

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "start": self.start.isoformat() if self.start else None,
            "end": self.end.isoformat() if self.end else None,
            "requests": self.requests,
            "fetched": self.fetched,
            "new_activities": self.new_activities,
            "last_sync_at": self.last_sync_at,
            "last_synced_day": self.last_synced_day,
            "message": self.message,
        }


def last_synced_day(conn: sqlite3.Connection) -> date | None:
    row = conn.execute(
        "SELECT MAX(key) FROM fetch_status"
        " WHERE endpoint = 'user_summary' AND status IN ('ok', 'empty')"
    ).fetchone()
    if row and row[0]:
        return date.fromisoformat(row[0])
    return None


def last_sync_at(conn: sqlite3.Connection) -> str | None:
    row = conn.execute(
        "SELECT MAX(finished_at) FROM sync_runs WHERE status = 'ok'"
    ).fetchone()
    return row[0] if row else None


def _freshness(conn: sqlite3.Connection) -> dict[str, str | None]:
    day = last_synced_day(conn)
    return {
        "last_sync_at": last_sync_at(conn),
        "last_synced_day": day.isoformat() if day else None,
    }


def sync_window(conn: sqlite3.Connection, today: date, settings: Settings) -> tuple[date, date] | str:
    last = last_synced_day(conn)
    if last is None:
        return "No synced data yet. Run `trainer backfill` in a terminal."
    gap = (today - last).days
    if gap > settings.gap_threshold_days:
        return (
            f"Last synced day is {last.isoformat()}, {gap} days ago."
            " Run `trainer backfill` in a terminal; it resumes and paces itself."
        )
    start = last - timedelta(days=settings.overlap_days)
    return (start, today)


def detail_endpoints(summary: dict) -> list[str]:
    result = ["activity_splits", "activity_hr_zones"]
    if summary.get("avgPower") is not None:
        result.append("activity_power_zones")
    type_key = (summary.get("activityType") or {}).get("typeKey", "")
    if type_key == "strength_training":
        result.append("activity_exercise_sets")
    return result


def _months_in_range(start: date, end: date) -> list[tuple[date, date]]:
    months = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        last_day = calendar.monthrange(cur.year, cur.month)[1]
        month_end = date(cur.year, cur.month, last_day)
        months.append((cur, month_end))
        if cur.month == 12:
            cur = date(cur.year + 1, 1, 1)
        else:
            cur = date(cur.year, cur.month + 1, 1)
    return months


def _months_descending(since: date, today: date) -> list[tuple[date, date]]:
    months = _months_in_range(since, today)
    months.reverse()
    return months


def _fetch_activity_details(
    conn: sqlite3.Connection,
    source: Source,
    activity_id: int,
    summary: dict,
    now: Any,
    fetched: dict[str, int],
    skip_done: bool = True,
) -> None:
    for ep in detail_endpoints(summary):
        key = str(activity_id)
        if skip_done and is_done(conn, ep, key):
            continue
        try:
            payload = source.activity(ep, activity_id)
            _store_and_normalize(conn, ep, key, payload, now, fetched)
        except FetchFailed as e:
            mark_error(conn, ep, key, str(e), now)


def _store_and_normalize(
    conn: sqlite3.Connection,
    endpoint: str,
    key: str,
    payload: Any,
    now: Any,
    fetched: dict[str, int],
) -> None:
    if store_response(conn, endpoint, key, payload, now) != "ok":
        return
    try:
        normalize(conn, endpoint, key, payload)
    except Exception as e:
        conn.rollback()
        mark_error(conn, endpoint, key, f"normalize: {type(e).__name__}", now)
        return
    fetched[endpoint] = fetched.get(endpoint, 0) + 1


def _ok_message(verb: str, start: date, end: date, fetched: dict[str, int], new_activities: int) -> str:
    return f"{verb} {start} to {end}: {sum(fetched.values())} responses, {new_activities} new activities."


def _failure_message(exc: Exception) -> str:
    return f"Sync failed: {type(exc).__name__}. Check the terminal with `trainer sync`."


def _skip_backfill_daily(conn: sqlite3.Connection, endpoint: str, key: str) -> bool:
    if is_done(conn, endpoint, key):
        return True
    row = conn.execute(
        "SELECT attempts FROM fetch_status WHERE endpoint = ? AND key = ? AND status = 'error'",
        (endpoint, key),
    ).fetchone()
    return row is not None and row[0] >= 3


def run_sync(
    conn: sqlite3.Connection,
    source_factory: Callable[[], Source],
    lock_path: Path,
    today: date,
    now: Any,
    settings: Settings,
    on_progress: Callable[[str], None] | None = None,
) -> SyncResult:
    try:
        with sync_lock(lock_path):
            window = sync_window(conn, today, settings)
            if isinstance(window, str):
                return SyncResult(
                    status="gap_too_large",
                    start=None,
                    end=None,
                    requests=0,
                    fetched={},
                    new_activities=0,
                    **_freshness(conn),
                    message=window,
                )

            start, end = window
            source = None
            fetched: dict[str, int] = {}
            new_activities = 0
            final_status = "error"
            final_message = ""

            run_id = conn.execute(
                "INSERT INTO sync_runs (kind, started_at, start_date, end_date, status, requests, message)"
                " VALUES ('sync', ?, ?, ?, 'running', 0, '')",
                (now.isoformat(), start.isoformat(), end.isoformat()),
            ).lastrowid
            conn.commit()

            try:
                source = source_factory()

                existing_keys = {
                    row[0]
                    for row in conn.execute(
                        "SELECT key FROM raw_responses WHERE endpoint = 'activity_summary'"
                    )
                }

                try:
                    activities = source.list_activities(start, end)
                except FetchFailed as e:
                    mark_error(conn, "activity_list", f"{start}..{end}", str(e), now)
                    activities = []

                for act in activities:
                    activity_id = act.get("activityId")
                    if activity_id is None:
                        continue
                    key = str(activity_id)
                    if key not in existing_keys:
                        new_activities += 1
                    _store_and_normalize(conn, "activity_summary", key, act, now, fetched)

                for act in activities:
                    activity_id = act.get("activityId")
                    if activity_id is None:
                        continue
                    _fetch_activity_details(conn, source, activity_id, act, now, fetched)

                for month_start, month_end in _months_in_range(start, end):
                    key = month_start.strftime("%Y-%m")
                    effective_end = min(month_end, today)
                    try:
                        payload = source.ftp_range(month_start, effective_end)
                        _store_and_normalize(conn, "ftp_range", key, payload, now, fetched)
                    except FetchFailed as e:
                        mark_error(conn, "ftp_range", key, str(e), now)

                day = start
                while day <= end:
                    for ep in DAILY_ENDPOINTS:
                        d_key = day.isoformat()
                        try:
                            payload = source.daily(ep, day)
                            _store_and_normalize(conn, ep, d_key, payload, now, fetched)
                        except FetchFailed as e:
                            mark_error(conn, ep, d_key, str(e), now)
                    day += timedelta(days=1)

                final_status = "ok"
                final_message = _ok_message("Synced", start, end, fetched, new_activities)

            except (AuthRequired, RateLimited) as e:
                final_status = "auth_required" if isinstance(e, AuthRequired) else "rate_limited"
                final_message = str(e)

            except Exception as e:
                final_message = _failure_message(e)

            finally:
                recompute_derived(conn)
                conn.execute(
                    "UPDATE sync_runs SET status = ?, finished_at = ?, requests = ?, message = ?"
                    " WHERE id = ?",
                    (final_status, now.isoformat(), source.requests if source else 0, final_message, run_id),
                )
                conn.commit()

            return SyncResult(
                status=final_status,
                start=start,
                end=end,
                requests=source.requests if source else 0,
                fetched=fetched,
                new_activities=new_activities,
                **_freshness(conn),
                message=final_message,
            )

    except SyncBusy as e:
        return SyncResult(
            status="busy",
            start=None,
            end=None,
            requests=0,
            fetched={},
            new_activities=0,
            **_freshness(conn),
            message=str(e),
        )


def run_backfill(
    conn: sqlite3.Connection,
    source_factory: Callable[[], Source],
    lock_path: Path,
    today: date,
    now: Any,
    settings: Settings,
    since: date | None = None,
    on_progress: Callable[[str], None] | None = None,
) -> SyncResult:
    if since is None:
        since = today - timedelta(days=settings.backfill_default_days)

    try:
        with sync_lock(lock_path):
            source = None
            fetched: dict[str, int] = {}
            new_activities = 0
            final_status = "error"
            final_message = ""

            run_id = conn.execute(
                "INSERT INTO sync_runs (kind, started_at, start_date, end_date, status, requests, message)"
                " VALUES ('backfill', ?, ?, ?, 'running', 0, '')",
                (now.isoformat(), since.isoformat(), today.isoformat()),
            ).lastrowid
            conn.commit()

            try:
                source = source_factory()

                existing_keys = {
                    row[0]
                    for row in conn.execute(
                        "SELECT key FROM raw_responses WHERE endpoint = 'activity_summary'"
                    )
                }

                if today.month == 1:
                    prev_month_start = date(today.year - 1, 12, 1)
                else:
                    prev_month_start = date(today.year, today.month - 1, 1)

                for month_start, month_end in _months_descending(since, today):
                    month_key = month_start.strftime("%Y-%m")
                    list_start = max(month_start, since)
                    list_end = min(month_end, today)

                    skip_list = (
                        is_done(conn, "activity_list", month_key)
                        and month_start < prev_month_start
                    )
                    if not skip_list:
                        try:
                            acts = source.list_activities(list_start, list_end)
                            full_month = list_start == month_start and list_end == month_end
                            if full_month and month_start < prev_month_start:
                                mark_status(conn, "activity_list", month_key, "ok", now)
                        except FetchFailed as e:
                            mark_error(conn, "activity_list", month_key, str(e), now)
                            acts = []

                        for act in acts:
                            activity_id = act.get("activityId")
                            if activity_id is None:
                                continue
                            key = str(activity_id)
                            if key not in existing_keys:
                                new_activities += 1
                            _store_and_normalize(conn, "activity_summary", key, act, now, fetched)

                    act_rows = conn.execute(
                        "SELECT activity_id, avg_power_w, type_key FROM activities"
                        " WHERE date >= ? AND date <= ?",
                        (list_start.isoformat(), list_end.isoformat()),
                    ).fetchall()

                    for row in act_rows:
                        mini = {
                            "avgPower": row[1],
                            "activityType": {"typeKey": row[2]},
                        }
                        for ep in detail_endpoints(mini):
                            key = str(row[0])
                            if is_done(conn, ep, key):
                                continue
                            err_row = conn.execute(
                                "SELECT attempts FROM fetch_status"
                                " WHERE endpoint = ? AND key = ? AND status = 'error'",
                                (ep, key),
                            ).fetchone()
                            if err_row and err_row[0] >= 3:
                                continue
                            try:
                                payload = source.activity(ep, row[0])
                                _store_and_normalize(conn, ep, key, payload, now, fetched)
                            except FetchFailed as e:
                                mark_error(conn, ep, key, str(e), now)

                    if not is_done(conn, "ftp_range", month_key):
                        effective_end = min(month_end, today)
                        try:
                            payload = source.ftp_range(month_start, effective_end)
                            _store_and_normalize(conn, "ftp_range", month_key, payload, now, fetched)
                        except FetchFailed as e:
                            mark_error(conn, "ftp_range", month_key, str(e), now)

                    day = list_end
                    while day >= list_start:
                        for ep in DAILY_ENDPOINTS:
                            d_key = day.isoformat()
                            if _skip_backfill_daily(conn, ep, d_key):
                                continue
                            try:
                                payload = source.daily(ep, day)
                                _store_and_normalize(conn, ep, d_key, payload, now, fetched)
                            except FetchFailed as e:
                                mark_error(conn, ep, d_key, str(e), now)
                        day -= timedelta(days=1)

                    recompute_derived(conn)
                    if on_progress:
                        on_progress(month_key)

                final_status = "ok"
                final_message = _ok_message("Backfilled", since, today, fetched, new_activities)

            except (AuthRequired, RateLimited) as e:
                final_status = "auth_required" if isinstance(e, AuthRequired) else "rate_limited"
                final_message = str(e)

            except Exception as e:
                final_message = _failure_message(e)

            finally:
                recompute_derived(conn)
                conn.execute(
                    "UPDATE sync_runs SET status = ?, finished_at = ?, requests = ?, message = ?"
                    " WHERE id = ?",
                    (final_status, now.isoformat(), source.requests if source else 0, final_message, run_id),
                )
                conn.commit()

            return SyncResult(
                status=final_status,
                start=since,
                end=today,
                requests=source.requests if source else 0,
                fetched=fetched,
                new_activities=new_activities,
                **_freshness(conn),
                message=final_message,
            )

    except SyncBusy as e:
        return SyncResult(
            status="busy",
            start=None,
            end=None,
            requests=0,
            fetched={},
            new_activities=0,
            **_freshness(conn),
            message=str(e),
        )
