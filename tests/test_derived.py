import copy
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from trainer.derived import ActivityWindow, find_duplicates, recompute_derived
from trainer.normalize import normalize

FIXTURES = Path(__file__).parent / "fixtures"
T0 = datetime(2026, 11, 9, 6, 0, 0)


def load_fixture(name: str):
    return json.loads((FIXTURES / f"{name}.json").read_text())


@pytest.fixture
def db_with_activities(db):
    items = load_fixture("activity_list")
    for item in items:
        normalize(db, "activity_summary", str(item["activityId"]), item)

    base = next(i for i in items if i["activityId"] == 1001)
    item_1004 = copy.deepcopy(base)
    item_1004["activityId"] = 1004
    item_1004["activityType"] = {"typeKey": "cycling"}
    item_1004["startTimeLocal"] = "2026-11-09 08:03:00"
    item_1004["startTimeGMT"] = "2026-11-09 06:03:00"
    item_1004["duration"] = base["duration"] * 0.95
    for key in ("avgPower", "normPower", "maxPower"):
        item_1004.pop(key, None)
    normalize(db, "activity_summary", "1004", item_1004)
    return db


def test_overlapping_same_family_marks_duplicate_preferring_power():
    edge = ActivityWindow(1, "cycling", T0, 3600, True)
    watch = ActivityWindow(2, "cycling", T0 + timedelta(minutes=5), 3500, False)
    assert find_duplicates([edge, watch]) == {2: 1}


def test_small_overlap_or_other_family_is_not_duplicate():
    a = ActivityWindow(1, "cycling", T0, 3600, True)
    b = ActivityWindow(2, "cycling", T0 + timedelta(minutes=50), 3600, True)
    c = ActivityWindow(3, "running", T0, 3600, False)
    assert find_duplicates([a, b, c]) == {}


def test_three_way_duplicate_groups_transitively():
    acts = [ActivityWindow(i, "cycling", T0 + timedelta(minutes=i), 3600, i == 3) for i in (1, 2, 3)]
    assert find_duplicates(acts) == {1: 3, 2: 3}


def test_recompute_derived_marks_duplicates(db_with_activities):
    recompute_derived(db_with_activities)
    rows = dict(db_with_activities.execute("SELECT activity_id, duplicate_of FROM activities").fetchall())
    assert rows[1004] == 1001 and rows[1001] is None
