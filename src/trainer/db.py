import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    """
CREATE TABLE raw_responses (
    endpoint TEXT NOT NULL,
    key TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (endpoint, key)
);
CREATE TABLE fetch_status (
    endpoint TEXT NOT NULL,
    key TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('ok','empty','error')),
    attempts INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (endpoint, key)
);
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    start_date TEXT,
    end_date TEXT,
    status TEXT NOT NULL,
    requests INTEGER NOT NULL DEFAULT 0,
    message TEXT
);
CREATE TABLE daily_metrics (
    date TEXT PRIMARY KEY,
    resting_hr INTEGER,
    stress_avg INTEGER,
    body_battery_high INTEGER,
    body_battery_low INTEGER,
    steps INTEGER,
    intensity_min_moderate INTEGER,
    intensity_min_vigorous INTEGER,
    sleep_s INTEGER,
    sleep_deep_s INTEGER,
    sleep_light_s INTEGER,
    sleep_rem_s INTEGER,
    sleep_awake_s INTEGER,
    sleep_score INTEGER,
    hrv_last_night_ms REAL,
    hrv_weekly_ms REAL,
    hrv_status TEXT,
    readiness_score INTEGER,
    readiness_level TEXT,
    readiness_morning_score INTEGER,
    training_status TEXT,
    acute_load REAL,
    chronic_load REAL,
    acwr REAL,
    load_aerobic_low REAL,
    load_aerobic_high REAL,
    load_anaerobic REAL,
    vo2max REAL,
    vo2max_cycling REAL,
    weight_kg REAL,
    body_fat_pct REAL,
    muscle_mass_kg REAL
);
CREATE TABLE ftp_history (
    date TEXT PRIMARY KEY,
    ftp_w REAL NOT NULL
);
CREATE TABLE activities (
    activity_id INTEGER PRIMARY KEY,
    name TEXT,
    type_key TEXT NOT NULL,
    sport_family TEXT NOT NULL,
    date TEXT NOT NULL,
    start_local TEXT,
    start_utc TEXT NOT NULL,
    duration_s REAL,
    moving_s REAL,
    distance_m REAL,
    elevation_gain_m REAL,
    avg_hr REAL,
    max_hr REAL,
    avg_power_w REAL,
    norm_power_w REAL,
    max_power_w REAL,
    calories REAL,
    aerobic_te REAL,
    anaerobic_te REAL,
    garmin_load REAL,
    device_id INTEGER,
    duplicate_of INTEGER
);
CREATE INDEX activities_date ON activities (date);
CREATE TABLE activity_laps (
    activity_id INTEGER NOT NULL,
    lap_index INTEGER NOT NULL,
    start_utc TEXT,
    duration_s REAL,
    distance_m REAL,
    avg_hr REAL,
    avg_power_w REAL,
    norm_power_w REAL,
    PRIMARY KEY (activity_id, lap_index)
);
CREATE TABLE activity_zones (
    activity_id INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK (kind IN ('hr','power')),
    zone INTEGER NOT NULL,
    seconds REAL,
    low_boundary REAL,
    PRIMARY KEY (activity_id, kind, zone)
);
CREATE TABLE exercise_sets (
    activity_id INTEGER NOT NULL,
    set_index INTEGER NOT NULL,
    set_type TEXT,
    category TEXT,
    exercise_name TEXT,
    reps INTEGER,
    weight_kg REAL,
    duration_s REAL,
    start_utc TEXT,
    PRIMARY KEY (activity_id, set_index)
);
CREATE VIEW v_activities AS
    SELECT * FROM activities WHERE duplicate_of IS NULL;
CREATE VIEW v_daily_status AS
    SELECT
        date, resting_hr, hrv_last_night_ms, hrv_weekly_ms, hrv_status,
        sleep_s, sleep_score, readiness_score, readiness_level,
        readiness_morning_score, body_battery_high, body_battery_low,
        stress_avg, training_status, acute_load, chronic_load, acwr,
        vo2max_cycling, weight_kg
    FROM daily_metrics;
""",
]


def migrate(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    for i, sql in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.executescript(sql)
        conn.execute(f"PRAGMA user_version = {i}")
        conn.commit()


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.row_factory = sqlite3.Row
    migrate(conn)
    return conn


def connect_ro(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.execute("PRAGMA query_only = ON")
    conn.row_factory = sqlite3.Row
    return conn
