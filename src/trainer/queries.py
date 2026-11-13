from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from datetime import date, timedelta

from trainer.db import connect_ro
from trainer.sync import last_sync_at, last_synced_day

COLUMN_NOTES: dict[str, str] = {
    "duplicate_of": (
        "activity_id of the canonical activity when this record was recorded twice"
        " (e.g., head unit + watch overlap)"
    ),
    "acwr": "Acute:Chronic Workload Ratio from Garmin, 7-day vs 28-day rolling load",
    "readiness_morning_score": "pre-training readiness score from Garmin, 0-100",
    "norm_power_w": (
        "Normalized Power in watts, a weighted average that reflects"
        " the physiological cost of variable-intensity effort"
    ),
}

_ALLOWED_ACTIONS = frozenset({
    sqlite3.SQLITE_SELECT,
    sqlite3.SQLITE_READ,
    sqlite3.SQLITE_FUNCTION,
    sqlite3.SQLITE_RECURSIVE,
})


class QueryError(Exception):
    pass


def freshness(conn: sqlite3.Connection) -> dict:
    oldest_row = conn.execute(
        "SELECT MIN(key) FROM fetch_status"
        " WHERE endpoint = 'user_summary' AND status IN ('ok', 'empty')"
    ).fetchone()
    oldest = oldest_row[0] if oldest_row else None

    last_at = last_sync_at(conn)
    last_day = last_synced_day(conn)
    return {
        "last_sync_at": last_at,
        "last_synced_day": last_day.isoformat() if last_day else None,
        "oldest_synced_day": oldest,
    }


def describe_schema(db_path) -> dict:
    conn = connect_ro(db_path)
    try:
        tables: dict = {}
        views: dict = {}
        for name, kind in conn.execute(
            "SELECT name, type FROM sqlite_master"
            " WHERE type IN ('table', 'view') ORDER BY name"
        ).fetchall():
            cols = [
                {"name": row[1], "type": row[2]}
                for row in conn.execute(f"PRAGMA table_info({name})").fetchall()
            ]
            if kind == "table":
                tables[name] = cols
            else:
                views[name] = cols
        return {
            "tables": tables,
            "views": views,
            "notes": COLUMN_NOTES,
            "freshness": freshness(conn),
        }
    finally:
        conn.close()


def query_sql(db_path, sql: str, max_rows: int, settings) -> dict:
    max_rows = max(1, min(max_rows, settings.sql_max_rows))
    conn = connect_ro(db_path)
    try:
        fresh = freshness(conn)
        deadline = time.monotonic() + settings.sql_timeout_s
        timed_out: list[bool] = []

        def _progress():
            if time.monotonic() > deadline:
                timed_out.append(True)
                return 1
            return None

        def _authorizer(action, *_):
            return sqlite3.SQLITE_OK if action in _ALLOWED_ACTIONS else sqlite3.SQLITE_DENY

        conn.set_authorizer(_authorizer)
        conn.set_progress_handler(_progress, 100)

        try:
            cur = conn.execute(sql)
            columns = [d[0] for d in (cur.description or [])]
            batch = cur.fetchmany(max_rows + 1)
            truncated = len(batch) > max_rows
            rows = [list(r) for r in batch[:max_rows]]
        except (sqlite3.DatabaseError, sqlite3.Warning) as e:
            if timed_out:
                raise QueryError(
                    f"query timed out after {settings.sql_timeout_s:g} s"
                )
            raise QueryError(str(e)) from e

        return {
            "columns": columns,
            "rows": rows,
            "row_count": len(rows),
            "truncated": truncated,
            "freshness": fresh,
        }
    finally:
        conn.close()


def daily_status(db_path, start: str, end: str) -> dict:
    try:
        start_d = date.fromisoformat(start)
        end_d = date.fromisoformat(end)
    except ValueError as e:
        raise QueryError(str(e)) from e

    conn = connect_ro(db_path)
    try:
        fresh = freshness(conn)
        rows = conn.execute(
            "SELECT * FROM v_daily_status WHERE date >= ? AND date <= ? ORDER BY date",
            (start, end),
        ).fetchall()
        rows_list = [dict(r) for r in rows]
        row_by_date = {r["date"]: r for r in rows_list}

        missing_days = []
        d = start_d
        while d <= end_d:
            ds = d.isoformat()
            if ds not in row_by_date:
                missing_days.append(ds)
            else:
                r = row_by_date[ds]
                if (
                    r["resting_hr"] is None
                    and r["sleep_s"] is None
                    and r["hrv_last_night_ms"] is None
                ):
                    missing_days.append(ds)
            d += timedelta(days=1)

        return {"rows": rows_list, "missing_days": missing_days, "freshness": fresh}
    finally:
        conn.close()


def list_activities(
    db_path,
    start: str,
    end: str,
    sport_family: str | None = None,
    include_duplicates: bool = False,
) -> dict:
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError as e:
        raise QueryError(str(e)) from e

    conn = connect_ro(db_path)
    try:
        fresh = freshness(conn)
        source = "activities" if include_duplicates else "v_activities"
        where = "date >= ? AND date <= ?"
        params: list = [start, end]
        if sport_family is not None:
            where += " AND sport_family = ?"
            params.append(sport_family)
        rows = conn.execute(
            f"SELECT * FROM {source} WHERE {where} ORDER BY date, start_utc",
            params,
        ).fetchall()
        return {"rows": [dict(r) for r in rows], "freshness": fresh}
    finally:
        conn.close()


def training_load(db_path, start: str, end: str) -> dict:
    try:
        date.fromisoformat(start)
        date.fromisoformat(end)
    except ValueError as e:
        raise QueryError(str(e)) from e

    conn = connect_ro(db_path)
    try:
        fresh = freshness(conn)
        rows = conn.execute(
            "SELECT date, sport_family, duration_s, distance_m,"
            " garmin_load, aerobic_te, anaerobic_te"
            " FROM v_activities WHERE date >= ? AND date <= ?",
            (start, end),
        ).fetchall()

        groups: dict[tuple, list] = defaultdict(list)
        for row in rows:
            d = date.fromisoformat(row["date"])
            year, week, _ = d.isocalendar()
            week_str = f"{year}-W{week:02d}"
            groups[(week_str, row["sport_family"])].append(row)

        weeks = []
        for (week_str, sf), group in sorted(groups.items()):
            durations = [r["duration_s"] for r in group if r["duration_s"] is not None]
            hours = round(sum(durations) / 3600, 2) if durations else None

            distances = [r["distance_m"] for r in group if r["distance_m"] is not None]
            distance_km = round(sum(distances) / 1000, 1) if distances else None

            garmin_loads = [r["garmin_load"] for r in group if r["garmin_load"] is not None]
            garmin_load = sum(garmin_loads) if garmin_loads else None

            aero_tes = [r["aerobic_te"] for r in group if r["aerobic_te"] is not None]
            aerobic_te_avg = round(sum(aero_tes) / len(aero_tes), 1) if aero_tes else None

            ana_tes = [r["anaerobic_te"] for r in group if r["anaerobic_te"] is not None]
            anaerobic_te_avg = round(sum(ana_tes) / len(ana_tes), 1) if ana_tes else None

            weeks.append({
                "week": week_str,
                "sport_family": sf,
                "hours": hours,
                "distance_km": distance_km,
                "activities": len(group),
                "garmin_load": garmin_load,
                "aerobic_te_avg": aerobic_te_avg,
                "anaerobic_te_avg": anaerobic_te_avg,
            })

        return {"weeks": weeks, "freshness": fresh}
    finally:
        conn.close()
