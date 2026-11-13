import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta


@dataclass(frozen=True)
class ActivityWindow:
    activity_id: int
    family: str
    start_utc: datetime
    duration_s: float
    has_power: bool


def _overlaps(a: ActivityWindow, b: ActivityWindow) -> bool:
    if a.family != b.family:
        return False
    a_end = a.start_utc + timedelta(seconds=a.duration_s)
    b_end = b.start_utc + timedelta(seconds=b.duration_s)
    overlap = (min(a_end, b_end) - max(a.start_utc, b.start_utc)).total_seconds()
    if overlap <= 0:
        return False
    return overlap / min(a.duration_s, b.duration_s) >= 0.5


def _canonical_key(a: ActivityWindow) -> tuple:
    return (not a.has_power, -a.duration_s, a.activity_id)


def find_duplicates(acts: list[ActivityWindow]) -> dict[int, int]:
    parent = {a.activity_id: a.activity_id for a in acts}

    def find(x: int) -> int:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x: int, y: int) -> None:
        parent[find(x)] = find(y)

    for i, a in enumerate(acts):
        for b in acts[i + 1:]:
            if _overlaps(a, b):
                union(a.activity_id, b.activity_id)

    groups: dict[int, list[ActivityWindow]] = {}
    for a in acts:
        groups.setdefault(find(a.activity_id), []).append(a)

    result: dict[int, int] = {}
    for group in groups.values():
        if len(group) < 2:
            continue
        canonical = min(group, key=_canonical_key)
        for a in group:
            if a.activity_id != canonical.activity_id:
                result[a.activity_id] = canonical.activity_id

    return result


def recompute_derived(conn: sqlite3.Connection) -> None:
    conn.execute("UPDATE activities SET duplicate_of = NULL")
    rows = conn.execute(
        "SELECT activity_id, sport_family, start_utc, duration_s, avg_power_w"
        " FROM activities"
        " WHERE start_utc IS NOT NULL AND duration_s IS NOT NULL"
    ).fetchall()

    acts = [
        ActivityWindow(
            activity_id=row[0],
            family=row[1],
            start_utc=datetime.fromisoformat(row[2]),
            duration_s=row[3],
            has_power=row[4] is not None,
        )
        for row in rows
    ]

    for dupe_id, canonical_id in find_duplicates(acts).items():
        conn.execute(
            "UPDATE activities SET duplicate_of = ? WHERE activity_id = ?",
            (canonical_id, dupe_id),
        )
    conn.commit()
