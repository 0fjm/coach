from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from typing import Any


class _FakeClient:
    def __init__(self) -> None:
        self.tokens: str = '{"token": "initial"}'

    def dumps(self) -> str:
        return self.tokens


class FakeGarmin:
    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple]] = []
        self.raise_on: dict[str, list[Exception]] = {}
        self.client = _FakeClient()

    def _call(self, name: str, args: tuple):
        if self.raise_on.get(name):
            exc = self.raise_on[name].pop(0)
            self.calls.append((name, args))
            raise exc
        self.calls.append((name, args))
        result: dict = {"method": name, "args": list(args)}
        if name == "get_activities_by_date":
            return [result]
        return result

    def get_user_summary(self, d: str):
        return self._call("get_user_summary", (d,))

    def get_sleep_data(self, d: str):
        return self._call("get_sleep_data", (d,))

    def get_hrv_data(self, d: str):
        return self._call("get_hrv_data", (d,))

    def get_training_readiness(self, d: str):
        return self._call("get_training_readiness", (d,))

    def get_training_status(self, d: str):
        return self._call("get_training_status", (d,))

    def get_body_composition(self, d: str):
        return self._call("get_body_composition", (d,))

    def get_activities_by_date(self, start: str, end: str):
        return self._call("get_activities_by_date", (start, end))

    def get_functional_threshold_power_range(self, start: str, end: str, *, sport: str = "RUNNING"):
        return self._call("get_functional_threshold_power_range", (start, end, sport))

    def get_activity_splits(self, activity_id: int):
        return self._call("get_activity_splits", (activity_id,))

    def get_activity_hr_in_timezones(self, activity_id: int):
        return self._call("get_activity_hr_in_timezones", (activity_id,))

    def get_activity_power_in_timezones(self, activity_id: int):
        return self._call("get_activity_power_in_timezones", (activity_id,))

    def get_activity_exercise_sets(self, activity_id: int):
        return self._call("get_activity_exercise_sets", (activity_id,))

    def login(self, *, tokenstore: str | None = None) -> None:
        pass


from trainer.garmin_source import DAILY_ENDPOINTS as _DAILY_ENDPOINTS

_FIXTURES_DIR = Path(__file__).parent / "fixtures"
_DAILY_ENDPOINT_NAMES = tuple(_DAILY_ENDPOINTS.keys())
_ACTIVITY_ENDPOINT_NAMES = (
    "activity_splits", "activity_hr_zones", "activity_power_zones",
    "activity_exercise_sets",
)


def _load_fixture(name: str) -> Any:
    return json.loads((_FIXTURES_DIR / f"{name}.json").read_text())


class FakeSource:
    def __init__(self, days: dict, activities: list[dict], raise_on: dict) -> None:
        self._days = days
        self._activities = activities
        self._raise_on = raise_on
        self._activity_fixtures: dict[str, Any] = {}
        self._ftp_fixture: Any = None
        self.calls: list[tuple] = []
        self.requests: int = 0

    def _check_raise(self, method: str, endpoint: str | None = None) -> None:
        exc = self._raise_on.get(method)
        if exc is not None:
            raise exc
        if endpoint is not None:
            exc = self._raise_on.get((method, endpoint))
            if exc is not None:
                raise exc

    def daily(self, endpoint: str, day: date) -> Any:
        self.calls.append(("daily", endpoint, day))
        self.requests += 1
        self._check_raise("daily", endpoint)
        return self._days.get(endpoint)

    def activity(self, endpoint: str, activity_id: int) -> Any:
        self.calls.append(("activity", endpoint, activity_id))
        self.requests += 1
        self._check_raise("activity", endpoint)
        return self._activity_fixtures.get(endpoint)

    def list_activities(self, start: date, end: date) -> list[dict]:
        self.calls.append(("list_activities", start, end))
        self.requests += 1
        self._check_raise("list_activities")
        return [
            act for act in self._activities
            if start <= date.fromisoformat(act["startTimeLocal"][:10]) <= end
        ]

    def ftp_range(self, start: date, end: date) -> Any:
        self.calls.append(("ftp_range", start, end))
        self.requests += 1
        self._check_raise("ftp_range")
        return self._ftp_fixture

    @classmethod
    def default(cls, raise_on: dict | None = None) -> "FakeSource":
        days = {ep: _load_fixture(ep) for ep in _DAILY_ENDPOINT_NAMES}
        activities = _load_fixture("activity_list")
        src = cls(days=days, activities=activities, raise_on=raise_on or {})
        for ep in _ACTIVITY_ENDPOINT_NAMES:
            src._activity_fixtures[ep] = _load_fixture(ep)
        src._ftp_fixture = _load_fixture("ftp_range")
        return src
