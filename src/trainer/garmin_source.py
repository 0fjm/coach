from __future__ import annotations

import time
from collections.abc import Callable
from datetime import date
from typing import Any, Protocol

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)


class AuthRequired(Exception):
    pass


class RateLimited(Exception):
    pass


class FetchFailed(Exception):
    pass


DAILY_ENDPOINTS: dict[str, Callable[[Any, str], Any]] = {
    "user_summary": lambda g, d: g.get_user_summary(d),
    "sleep": lambda g, d: g.get_sleep_data(d),
    "hrv": lambda g, d: g.get_hrv_data(d),
    "training_readiness": lambda g, d: g.get_training_readiness(d),
    "training_status": lambda g, d: g.get_training_status(d),
    "body_composition": lambda g, d: g.get_body_composition(d),
}

ACTIVITY_ENDPOINTS: dict[str, Callable[[Any, int], Any]] = {
    "activity_splits": lambda g, aid: g.get_activity_splits(aid),
    "activity_hr_zones": lambda g, aid: g.get_activity_hr_in_timezones(aid),
    "activity_power_zones": lambda g, aid: g.get_activity_power_in_timezones(aid),
    "activity_exercise_sets": lambda g, aid: g.get_activity_exercise_sets(aid),
}


class Source(Protocol):
    requests: int

    def daily(self, endpoint: str, day: date) -> Any | None: ...
    def activity(self, endpoint: str, activity_id: int) -> Any | None: ...
    def list_activities(self, start: date, end: date) -> list[dict]: ...
    def ftp_range(self, start: date, end: date) -> Any | None: ...


class Pacer:
    def __init__(
        self,
        interval_s: float,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._interval = interval_s
        self._clock = clock
        self._sleep = sleep
        self._last: float | None = None

    def wait(self) -> None:
        now = self._clock()
        if self._last is not None:
            gap = self._interval - (now - self._last)
            if gap > 0:
                self._sleep(gap)
        self._last = self._clock()


class GarminSource:
    def __init__(
        self,
        garmin: Any,
        pacer: Pacer,
        backoff_s: tuple[float, ...],
        on_tokens_changed: Callable[[str], None],
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._garmin = garmin
        self._pacer = pacer
        self._backoff_s = backoff_s
        self._on_tokens_changed = on_tokens_changed
        self._sleep = sleep
        self._last_tokens: str = garmin.client.dumps()
        self.requests: int = 0

    def _check_tokens(self) -> None:
        current = self._garmin.client.dumps()
        if current != self._last_tokens:
            self._last_tokens = current
            self._on_tokens_changed(current)

    def _attempt(self, thunk: Callable[[], Any]) -> Any | None:
        """Run thunk once, check tokens, and apply full error mapping."""
        try:
            result = thunk()
            self._check_tokens()
            return result
        except GarminConnectNotFoundError:
            self._check_tokens()
            return None
        except GarminConnectAuthenticationError:
            self._check_tokens()
            raise AuthRequired(
                "Garmin session expired. Run `trainer login` in a terminal."
            )
        except GarminConnectTooManyRequestsError:
            self._check_tokens()
            raise
        except GarminConnectConnectionError as e:
            self._check_tokens()
            response = getattr(e, "response", None)
            status = getattr(response, "status_code", None)
            msg = type(e).__name__ if status is None else f"{type(e).__name__} {status}"
            raise FetchFailed(msg)

    def _call(self, thunk: Callable[[], Any]) -> Any | None:
        self._pacer.wait()
        self.requests += 1
        try:
            return self._attempt(thunk)
        except GarminConnectTooManyRequestsError:
            for delay in self._backoff_s:
                self._sleep(delay)
                self.requests += 1
                try:
                    return self._attempt(thunk)
                except GarminConnectTooManyRequestsError:
                    continue
            raise RateLimited()

    def daily(self, endpoint: str, day: date) -> Any | None:
        fn = DAILY_ENDPOINTS[endpoint]
        d = day.isoformat()
        return self._call(lambda: fn(self._garmin, d))

    def activity(self, endpoint: str, activity_id: int) -> Any | None:
        fn = ACTIVITY_ENDPOINTS[endpoint]
        return self._call(lambda: fn(self._garmin, activity_id))

    def list_activities(self, start: date, end: date) -> list[dict]:
        result = self._call(
            lambda: self._garmin.get_activities_by_date(
                start.isoformat(), end.isoformat()
            )
        )
        return result if result is not None else []

    def ftp_range(self, start: date, end: date) -> Any | None:
        return self._call(
            lambda: self._garmin.get_functional_threshold_power_range(
                start.isoformat(), end.isoformat(), sport="CYCLING"
            )
        )
