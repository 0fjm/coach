import json
import sqlite3
from typing import Any


def _get(d: Any, *path: str) -> Any:
    for k in path:
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


def _device_entries(value: Any) -> list[dict]:
    if isinstance(value, dict):
        value = list(value.values())
    if not isinstance(value, list):
        return []
    return [e for e in value if isinstance(e, dict)]


def _primary_device(entries: list[dict]) -> dict | None:
    return next((e for e in entries if e.get("primaryTrainingDevice")), entries[0] if entries else None)


def sport_family(type_key: str) -> str:
    k = type_key.lower()
    if any(w in k for w in ("cycling", "biking", "ride")):
        return "cycling"
    if "running" in k:
        return "running"
    if k == "strength_training":
        return "strength"
    return type_key


def _upsert_daily(conn: sqlite3.Connection, date_key: str, **cols: Any) -> None:
    names = list(cols.keys())
    vals = [cols[n] for n in names]
    ph = ", ".join("?" for _ in names)
    col_names = ", ".join(names)
    update_set = ", ".join(f"{n} = excluded.{n}" for n in names)
    conn.execute(
        f"INSERT INTO daily_metrics (date, {col_names}) VALUES (?, {ph})"
        f" ON CONFLICT(date) DO UPDATE SET {update_set}",
        [date_key] + vals,
    )
    conn.commit()


def _normalize_user_summary(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    _upsert_daily(
        conn, key,
        resting_hr=_get(payload, "restingHeartRate"),
        stress_avg=_get(payload, "averageStressLevel"),
        body_battery_high=_get(payload, "bodyBatteryHighestValue"),
        body_battery_low=_get(payload, "bodyBatteryLowestValue"),
        steps=_get(payload, "totalSteps"),
        intensity_min_moderate=_get(payload, "moderateIntensityMinutes"),
        intensity_min_vigorous=_get(payload, "vigorousIntensityMinutes"),
    )


def _normalize_sleep(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    dto = _get(payload, "dailySleepDTO")
    _upsert_daily(
        conn, key,
        sleep_s=_get(dto, "sleepTimeSeconds"),
        sleep_deep_s=_get(dto, "deepSleepSeconds"),
        sleep_light_s=_get(dto, "lightSleepSeconds"),
        sleep_rem_s=_get(dto, "remSleepSeconds"),
        sleep_awake_s=_get(dto, "awakeSleepSeconds"),
        sleep_score=_get(dto, "sleepScores", "overall", "value"),
    )


def _normalize_hrv(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    summary = _get(payload, "hrvSummary")
    _upsert_daily(
        conn, key,
        hrv_last_night_ms=_get(summary, "lastNightAvg"),
        hrv_weekly_ms=_get(summary, "weeklyAvg"),
        hrv_status=_get(summary, "status"),
    )


def _normalize_training_readiness(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    entries = payload if isinstance(payload, list) and payload else []
    if entries:
        sorted_entries = sorted(entries, key=lambda e: e.get("timestamp", ""))
        earliest = sorted_entries[0]
        latest = sorted_entries[-1]
        _upsert_daily(
            conn, key,
            readiness_score=_get(latest, "score"),
            readiness_level=_get(latest, "level"),
            readiness_morning_score=_get(earliest, "score"),
        )
    else:
        _upsert_daily(
            conn, key,
            readiness_score=None,
            readiness_level=None,
            readiness_morning_score=None,
        )


def _normalize_training_status(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    device = _primary_device(_device_entries(
        _get(payload, "mostRecentTrainingStatus", "latestTrainingStatusData")
    ))
    atl = _get(device, "acuteTrainingLoadDTO")
    load_device = _primary_device(_device_entries(
        _get(payload, "mostRecentTrainingLoadBalance", "metricsTrainingLoadBalanceDTOMap")
    ))

    _upsert_daily(
        conn, key,
        training_status=_get(device, "trainingStatusFeedbackPhrase"),
        acute_load=_get(atl, "dailyTrainingLoadAcute"),
        chronic_load=_get(atl, "dailyTrainingLoadChronic"),
        acwr=_get(atl, "dailyAcuteChronicWorkloadRatio"),
        load_aerobic_low=_get(load_device, "monthlyLoadAerobicLow"),
        load_aerobic_high=_get(load_device, "monthlyLoadAerobicHigh"),
        load_anaerobic=_get(load_device, "monthlyLoadAnaerobic"),
        vo2max=_get(payload, "mostRecentVO2Max", "generic", "vo2MaxPreciseValue"),
        vo2max_cycling=_get(payload, "mostRecentVO2Max", "cycling", "vo2MaxPreciseValue"),
    )


def _normalize_body_composition(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    raw_weight = _get(payload, "totalAverage", "weight")
    raw_muscle = _get(payload, "totalAverage", "muscleMass")
    _upsert_daily(
        conn, key,
        weight_kg=raw_weight / 1000 if raw_weight is not None else None,
        body_fat_pct=_get(payload, "totalAverage", "bodyFat"),
        muscle_mass_kg=raw_muscle / 1000 if raw_muscle is not None else None,
    )


def _normalize_ftp_range(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    if isinstance(payload, dict):
        payload = next((v for v in payload.values() if isinstance(v, list)), [])
    if not isinstance(payload, list):
        payload = []
    for entry in payload:
        day = _get(entry, "calendarDate")
        value = _get(entry, "value")
        if day is not None and value is not None:
            conn.execute(
                "INSERT INTO ftp_history (date, ftp_w) VALUES (?, ?)"
                " ON CONFLICT(date) DO UPDATE SET ftp_w = excluded.ftp_w",
                (day, value),
            )
    conn.commit()


def _normalize_activity_summary(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    type_key = _get(payload, "activityType", "typeKey") or ""
    sf = sport_family(type_key)
    start_local = _get(payload, "startTimeLocal")
    date = start_local[:10] if start_local else None
    conn.execute(
        """
        INSERT INTO activities (
            activity_id, name, type_key, sport_family, date, start_local, start_utc,
            duration_s, moving_s, distance_m, elevation_gain_m, avg_hr, max_hr,
            avg_power_w, norm_power_w, max_power_w, calories, aerobic_te, anaerobic_te,
            garmin_load, device_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(activity_id) DO UPDATE SET
            name = excluded.name,
            type_key = excluded.type_key,
            sport_family = excluded.sport_family,
            date = excluded.date,
            start_local = excluded.start_local,
            start_utc = excluded.start_utc,
            duration_s = excluded.duration_s,
            moving_s = excluded.moving_s,
            distance_m = excluded.distance_m,
            elevation_gain_m = excluded.elevation_gain_m,
            avg_hr = excluded.avg_hr,
            max_hr = excluded.max_hr,
            avg_power_w = excluded.avg_power_w,
            norm_power_w = excluded.norm_power_w,
            max_power_w = excluded.max_power_w,
            calories = excluded.calories,
            aerobic_te = excluded.aerobic_te,
            anaerobic_te = excluded.anaerobic_te,
            garmin_load = excluded.garmin_load,
            device_id = excluded.device_id
        """,
        (
            _get(payload, "activityId"),
            _get(payload, "activityName"),
            type_key,
            sf,
            date,
            start_local,
            _get(payload, "startTimeGMT"),
            _get(payload, "duration"),
            _get(payload, "movingDuration"),
            _get(payload, "distance"),
            _get(payload, "elevationGain"),
            _get(payload, "averageHR"),
            _get(payload, "maxHR"),
            _get(payload, "avgPower"),
            _get(payload, "normPower"),
            _get(payload, "maxPower"),
            _get(payload, "calories"),
            _get(payload, "aerobicTrainingEffect"),
            _get(payload, "anaerobicTrainingEffect"),
            _get(payload, "activityTrainingLoad"),
            _get(payload, "deviceId"),
        ),
    )
    conn.commit()


def _normalize_activity_splits(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    activity_id = int(key)
    conn.execute("DELETE FROM activity_laps WHERE activity_id = ?", (activity_id,))
    for i, lap in enumerate((_get(payload, "lapDTOs") or [])):
        conn.execute(
            "INSERT INTO activity_laps (activity_id, lap_index, start_utc, duration_s, distance_m, avg_hr, avg_power_w, norm_power_w)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity_id, i,
                _get(lap, "startTimeGMT"),
                _get(lap, "duration"),
                _get(lap, "distance"),
                _get(lap, "averageHR"),
                _get(lap, "averagePower"),
                _get(lap, "normalizedPower"),
            ),
        )
    conn.commit()


def _normalize_zones(conn: sqlite3.Connection, key: str, payload: Any, kind: str) -> None:
    activity_id = int(key)
    conn.execute("DELETE FROM activity_zones WHERE activity_id = ? AND kind = ?", (activity_id, kind))
    for zone in (payload or []):
        conn.execute(
            "INSERT INTO activity_zones (activity_id, kind, zone, seconds, low_boundary) VALUES (?, ?, ?, ?, ?)",
            (
                activity_id, kind,
                _get(zone, "zoneNumber"),
                _get(zone, "secsInZone"),
                _get(zone, "zoneLowBoundary"),
            ),
        )
    conn.commit()


def _normalize_activity_hr_zones(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    _normalize_zones(conn, key, payload, "hr")


def _normalize_activity_power_zones(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    _normalize_zones(conn, key, payload, "power")


def _normalize_activity_exercise_sets(conn: sqlite3.Connection, key: str, payload: Any) -> None:
    activity_id = int(key)
    conn.execute("DELETE FROM exercise_sets WHERE activity_id = ?", (activity_id,))
    for i, s in enumerate((_get(payload, "exerciseSets") or [])):
        exercises = s.get("exercises") or []
        ex = exercises[0] if exercises else {}
        raw_weight = _get(s, "weight")
        conn.execute(
            "INSERT INTO exercise_sets (activity_id, set_index, set_type, category, exercise_name, reps, weight_kg, duration_s, start_utc)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                activity_id, i,
                _get(s, "setType"),
                _get(ex, "category"),
                _get(ex, "name"),
                _get(s, "repetitionCount"),
                raw_weight / 1000 if raw_weight is not None else None,
                _get(s, "duration"),
                _get(s, "startTime"),
            ),
        )
    conn.commit()


_HANDLERS = {
    "user_summary": _normalize_user_summary,
    "sleep": _normalize_sleep,
    "hrv": _normalize_hrv,
    "training_readiness": _normalize_training_readiness,
    "training_status": _normalize_training_status,
    "body_composition": _normalize_body_composition,
    "ftp_range": _normalize_ftp_range,
    "activity_summary": _normalize_activity_summary,
    "activity_splits": _normalize_activity_splits,
    "activity_hr_zones": _normalize_activity_hr_zones,
    "activity_power_zones": _normalize_activity_power_zones,
    "activity_exercise_sets": _normalize_activity_exercise_sets,
}


def normalize(conn: sqlite3.Connection, endpoint: str, key: str, payload: Any) -> None:
    handler = _HANDLERS.get(endpoint)
    if handler is None:
        return
    handler(conn, key, payload)


def rebuild(conn: sqlite3.Connection) -> int:
    for table in ("daily_metrics", "ftp_history", "activities", "activity_laps", "activity_zones", "exercise_sets"):
        conn.execute(f"DELETE FROM {table}")
    conn.commit()

    rows = conn.execute(
        "SELECT endpoint, key, payload FROM raw_responses"
        " ORDER BY CASE WHEN endpoint = 'activity_summary' THEN 0 ELSE 1 END, rowid"
    ).fetchall()

    for row in rows:
        normalize(conn, row[0], row[1], json.loads(row[2]))

    from trainer.derived import recompute_derived
    recompute_derived(conn)

    return len(rows)
