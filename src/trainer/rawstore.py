import json
import sqlite3
from datetime import datetime
from typing import Any


def store_response(
    conn: sqlite3.Connection,
    endpoint: str,
    key: str,
    payload: Any,
    now: datetime,
) -> str:
    if payload is None or payload == {} or payload == []:
        conn.execute(
            """
            INSERT INTO fetch_status (endpoint, key, status, updated_at)
            VALUES (?, ?, 'empty', ?)
            ON CONFLICT (endpoint, key) DO UPDATE SET
                status = 'empty',
                updated_at = excluded.updated_at
            """,
            (endpoint, key, now.isoformat()),
        )
        conn.commit()
        return "empty"
    conn.execute(
        "INSERT OR REPLACE INTO raw_responses (endpoint, key, fetched_at, payload) VALUES (?, ?, ?, ?)",
        (endpoint, key, now.isoformat(), json.dumps(payload)),
    )
    conn.execute(
        """
        INSERT INTO fetch_status (endpoint, key, status, updated_at)
        VALUES (?, ?, 'ok', ?)
        ON CONFLICT (endpoint, key) DO UPDATE SET
            status = 'ok',
            updated_at = excluded.updated_at
        """,
        (endpoint, key, now.isoformat()),
    )
    conn.commit()
    return "ok"


def mark_status(
    conn: sqlite3.Connection,
    endpoint: str,
    key: str,
    status: str,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_status (endpoint, key, status, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (endpoint, key) DO UPDATE SET
            status = excluded.status,
            updated_at = excluded.updated_at
        """,
        (endpoint, key, status, now.isoformat()),
    )
    conn.commit()


def mark_error(
    conn: sqlite3.Connection,
    endpoint: str,
    key: str,
    error: str,
    now: datetime,
) -> None:
    conn.execute(
        """
        INSERT INTO fetch_status (endpoint, key, status, attempts, last_error, updated_at)
        VALUES (?, ?, 'error', 1, ?, ?)
        ON CONFLICT (endpoint, key) DO UPDATE SET
            status = 'error',
            attempts = attempts + 1,
            last_error = excluded.last_error,
            updated_at = excluded.updated_at
        """,
        (endpoint, key, error[:200], now.isoformat()),
    )
    conn.commit()


def is_done(conn: sqlite3.Connection, endpoint: str, key: str) -> bool:
    row = conn.execute(
        "SELECT status FROM fetch_status WHERE endpoint = ? AND key = ?",
        (endpoint, key),
    ).fetchone()
    return row is not None and row[0] in ("ok", "empty")
