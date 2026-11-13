import re
from datetime import date
from pathlib import Path

import garminconnect
import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from fakes import FakeGarmin
from trainer.garmin_source import (
    ACTIVITY_ENDPOINTS,
    DAILY_ENDPOINTS,
    AuthRequired,
    FetchFailed,
    GarminSource,
    Pacer,
    RateLimited,
)

SRC = Path(__file__).resolve().parents[1] / "src"


@pytest.fixture
def fake():
    return FakeGarmin()


@pytest.fixture
def slept():
    return []


@pytest.fixture
def saved_tokens():
    return []


@pytest.fixture
def source(fake, slept, saved_tokens):
    return GarminSource(
        garmin=fake,
        pacer=Pacer(0.0),
        backoff_s=(60.0, 120.0, 240.0),
        on_tokens_changed=saved_tokens.append,
        sleep=slept.append,
    )


def test_daily_calls_mapped_method_with_iso_date(source, fake):
    source.daily("sleep", date(2026, 11, 10))
    assert fake.calls == [("get_sleep_data", ("2026-11-10",))]


def test_not_found_returns_none(source, fake):
    fake.raise_on["get_hrv_data"] = [GarminConnectNotFoundError("404")]
    assert source.daily("hrv", date(2026, 11, 10)) is None


def test_auth_error_raises_auth_required(source, fake):
    fake.raise_on["get_user_summary"] = [GarminConnectAuthenticationError("401")]
    with pytest.raises(AuthRequired, match="trainer login"):
        source.daily("user_summary", date(2026, 11, 10))


def test_rate_limit_backs_off_then_stops(source, fake, slept):
    fake.raise_on["get_sleep_data"] = [GarminConnectTooManyRequestsError()] * 4
    with pytest.raises(RateLimited):
        source.daily("sleep", date(2026, 11, 10))
    assert slept == [60.0, 120.0, 240.0]
    assert len(fake.calls) == 4


def test_rate_limit_recovers(source, fake, slept):
    fake.raise_on["get_sleep_data"] = [GarminConnectTooManyRequestsError()]
    assert source.daily("sleep", date(2026, 11, 10)) is not None
    assert slept == [60.0]


def test_pacer_spaces_requests():
    t = [0.0]
    waits = []
    pacer = Pacer(1.0, clock=lambda: t[0], sleep=waits.append)
    pacer.wait()
    t[0] = 0.3
    pacer.wait()
    assert waits == [pytest.approx(0.7)]


def test_tokens_persisted_when_rotated(source, fake, saved_tokens):
    source.daily("sleep", date(2026, 11, 10))
    assert saved_tokens == []
    fake.client.tokens = '{"di_token": "new"}'
    source.daily("sleep", date(2026, 11, 10))
    assert saved_tokens == ['{"di_token": "new"}']


def test_rate_limit_then_not_found_returns_none(source, fake, slept):
    fake.raise_on["get_sleep_data"] = [
        GarminConnectTooManyRequestsError(),
        GarminConnectNotFoundError("404"),
    ]
    assert source.daily("sleep", date(2026, 11, 10)) is None
    assert slept == [60.0]


def test_rate_limit_then_auth_error_raises_auth_required(source, fake, slept):
    fake.raise_on["get_sleep_data"] = [
        GarminConnectTooManyRequestsError(),
        GarminConnectAuthenticationError("401"),
    ]
    with pytest.raises(AuthRequired, match="trainer login"):
        source.daily("sleep", date(2026, 11, 10))
    assert slept == [60.0]


def test_rate_limit_then_connection_error_raises_fetch_failed(source, fake, slept):
    from garminconnect import GarminConnectConnectionError

    fake.raise_on["get_sleep_data"] = [
        GarminConnectTooManyRequestsError(),
        GarminConnectConnectionError("err"),
    ]
    with pytest.raises(FetchFailed):
        source.daily("sleep", date(2026, 11, 10))
    assert slept == [60.0]


def test_tokens_saved_on_connection_error(source, fake, saved_tokens):
    from garminconnect import GarminConnectConnectionError

    fake.client.tokens = '{"di_token": "rotated"}'
    fake.raise_on["get_sleep_data"] = [GarminConnectConnectionError("err")]
    with pytest.raises(FetchFailed):
        source.daily("sleep", date(2026, 11, 10))
    assert saved_tokens == ['{"di_token": "rotated"}']


def test_registry_uses_only_read_methods():
    names = [f.__code__.co_names for f in (*DAILY_ENDPOINTS.values(), *ACTIVITY_ENDPOINTS.values())]
    assert all(n.startswith("get_") for ns in names for n in ns)


def test_no_garmin_write_method_appears_in_source():
    writes = [
        n for n in dir(garminconnect.Garmin)
        if n.split("_")[0] in {"upload", "delete", "add", "set", "create", "update", "schedule", "import", "move", "request"}
    ]
    text = "\n".join(p.read_text() for p in SRC.rglob("*.py"))
    assert [n for n in writes if f".{n}(" in text] == []


def test_garminconnect_imported_only_in_source_and_auth():
    importers = {
        p.name for p in SRC.rglob("*.py")
        if re.search(r"^\s*(from|import) garminconnect", p.read_text(), re.M)
    }
    assert importers <= {"garmin_source.py", "auth.py"}
