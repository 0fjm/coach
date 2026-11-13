import re
import stat
from pathlib import Path

import pytest
from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from fakes import FakeGarmin
from trainer.auth import (
    FileTokenStore,
    KeychainTokenStore,
    LoginFailed,
    LoginRateLimited,
    TokenStoreUnavailable,
    load_tokens,
    open_source,
    interactive_login,
)
from trainer.config import Paths, Settings
from trainer.garmin_source import AuthRequired, RateLimited

TOKENS = '{"token": "initial"}'


class BrokenStore:
    name = "broken"

    def load(self) -> str | None:
        raise TokenStoreUnavailable("broken")

    def save(self, tokens: str) -> None:
        pass

    def delete(self) -> None:
        pass


class RecordingFactory:
    def __init__(self, login_error=None):
        self.login_error = login_error
        self.init_kwargs: list[dict] = []
        self.login_calls: list[dict] = []

    def __call__(self, **kwargs):
        self.init_kwargs.append(kwargs)
        fg = FakeGarmin()
        err = self.login_error
        factory = self

        def login(**lkwargs):
            factory.login_calls.append(lkwargs)
            if err is not None:
                raise err

        fg.login = login
        return fg


@pytest.fixture
def paths_no_tokens(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    return Paths(
        root=root,
        db=root / "data" / "trainer.db",
        plans=root / "plans",
        secrets=root / ".secrets",
        token_file=root / ".secrets" / "garmin.token",
        lock=root / "data" / "sync.lock",
    )


@pytest.fixture
def paths_with_tokens(tmp_path):
    root = tmp_path / "home"
    root.mkdir()
    token_file = root / ".secrets" / "garmin.token"
    FileTokenStore(token_file).save(TOKENS)
    return Paths(
        root=root,
        db=root / "data" / "trainer.db",
        plans=root / "plans",
        secrets=root / ".secrets",
        token_file=token_file,
        lock=root / "data" / "sync.lock",
    )


def test_auth_has_no_toplevel_keyring_import():
    src = Path(__file__).resolve().parents[1] / "src" / "trainer" / "auth.py"
    for lineno, line in enumerate(src.read_text().splitlines(), 1):
        if re.match(r"^(import keyring|from keyring)", line):
            pytest.fail(f"auth.py line {lineno}: top-level keyring import: {line!r}")


def test_file_store_permissions_and_roundtrip(tmp_path):
    store = FileTokenStore(tmp_path / ".secrets" / "garmin.token")
    store.save('{"di_token": "t"}')
    assert stat.S_IMODE((tmp_path / ".secrets").stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / ".secrets" / "garmin.token").stat().st_mode) == 0o600
    assert store.load() == '{"di_token": "t"}'


def test_keychain_store_roundtrip(memory_keyring):
    store = KeychainTokenStore(backend=memory_keyring)
    store.save("tok")
    assert store.load() == "tok"
    store.delete()
    assert store.load() is None


def test_load_tokens_prefers_first_store_and_skips_broken(tmp_path):
    file_store = FileTokenStore(tmp_path / "t.token")
    file_store.save("from-file")
    assert load_tokens([BrokenStore(), file_store]) == ("from-file", file_store)


def test_open_source_without_tokens_requires_login(paths_no_tokens):
    with pytest.raises(AuthRequired, match="trainer login"):
        open_source(paths_no_tokens, Settings(), garmin_factory=RecordingFactory())


def test_open_source_never_passes_credentials_and_never_retries(paths_with_tokens):
    factory = RecordingFactory(login_error=GarminConnectAuthenticationError("401"))
    with pytest.raises(AuthRequired):
        open_source(paths_with_tokens, Settings(), garmin_factory=factory)
    assert factory.init_kwargs == [{}]
    assert factory.login_calls == [{"tokenstore": TOKENS}]


def test_interactive_login_saves_tokens_and_clears_other_store(tmp_path, memory_keyring):
    file_store = FileTokenStore(tmp_path / "t.token")
    file_store.save("stale")
    interactive_login(
        KeychainTokenStore(backend=memory_keyring), [file_store],
        "a@b.c", "pw", lambda: "123",
        garmin_factory=RecordingFactory(),
    )
    assert KeychainTokenStore(backend=memory_keyring).load() == TOKENS
    assert file_store.load() is None
    assert "pw" not in TOKENS


def test_interactive_login_roundtrip_failure_raises_and_keeps_other_store(tmp_path):
    class _NullReadStore:
        name = "null-read"

        def load(self) -> str | None:
            return None

        def save(self, tokens: str) -> None:
            pass

        def delete(self) -> None:
            pass

    other = FileTokenStore(tmp_path / "other.token")
    other.save("keep-me")
    with pytest.raises(TokenStoreUnavailable, match="null-read"):
        interactive_login(
            _NullReadStore(), [other], "a@b.c", "pw", lambda: "1",
            garmin_factory=RecordingFactory(),
        )
    assert other.load() == "keep-me"


def test_interactive_login_rate_limited_is_single_attempt(memory_keyring):
    factory = RecordingFactory(login_error=GarminConnectTooManyRequestsError())
    with pytest.raises(LoginRateLimited):
        interactive_login(
            KeychainTokenStore(backend=memory_keyring), [], "a@b.c", "pw", lambda: "1",
            garmin_factory=factory,
        )
    assert len(factory.login_calls) == 1


def test_open_source_rate_limited_login_maps_to_rate_limited(paths_with_tokens):
    factory = RecordingFactory(login_error=GarminConnectTooManyRequestsError())
    with pytest.raises(RateLimited):
        open_source(paths_with_tokens, Settings(), garmin_factory=factory)


@pytest.mark.parametrize("error", [GarminConnectAuthenticationError, GarminConnectConnectionError])
def test_interactive_login_failure_raises_login_failed(memory_keyring, error):
    factory = RecordingFactory(login_error=error("pw in text"))
    with pytest.raises(LoginFailed) as info:
        interactive_login(
            KeychainTokenStore(backend=memory_keyring), [], "a@b.c", "pw", lambda: "1",
            garmin_factory=factory,
        )
    assert "pw" not in str(info.value) and info.value.__suppress_context__


def test_file_store_replaces_atomically_and_keeps_mode(tmp_path):
    path = tmp_path / ".secrets" / "garmin.token"
    store = FileTokenStore(path)
    store.save("first")
    path.chmod(0o644)
    store.save("second")
    assert store.load() == "second"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert [p.name for p in path.parent.iterdir()] == ["garmin.token"]
