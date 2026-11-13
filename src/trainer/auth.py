from __future__ import annotations

import os
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any, Protocol

from garminconnect import (
    Garmin,
    GarminConnectAuthenticationError,
    GarminConnectConnectionError,
    GarminConnectTooManyRequestsError,
)

from trainer.config import Paths, Settings
from trainer.garmin_source import AuthRequired, GarminSource, Pacer, RateLimited


class TokenStore(Protocol):
    name: str

    def load(self) -> str | None: ...
    def save(self, tokens: str) -> None: ...
    def delete(self) -> None: ...


class TokenStoreUnavailable(Exception):
    pass


class KeychainTokenStore:
    name = "keychain"

    def __init__(
        self,
        service: str = "personal-trainer-garmin",
        username: str = "garmin",
        backend: Any = None,
    ) -> None:
        self._service = service
        self._username = username
        self._backend = backend

    def load(self) -> str | None:
        if self._backend is not None:
            return self._backend.get_password(self._service, self._username)
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as e:
            raise TokenStoreUnavailable("keyring not available") from e
        try:
            return keyring.get_password(self._service, self._username)
        except KeyringError as e:
            raise TokenStoreUnavailable() from e

    def save(self, tokens: str) -> None:
        if self._backend is not None:
            self._backend.set_password(self._service, self._username, tokens)
            return
        try:
            import keyring
            from keyring.errors import KeyringError
        except ImportError as e:
            raise TokenStoreUnavailable("keyring not available") from e
        try:
            keyring.set_password(self._service, self._username, tokens)
        except KeyringError as e:
            raise TokenStoreUnavailable() from e

    def delete(self) -> None:
        if self._backend is not None:
            self._backend.delete_password(self._service, self._username)
            return
        try:
            import keyring
            from keyring.errors import KeyringError, PasswordDeleteError
        except ImportError as e:
            raise TokenStoreUnavailable("keyring not available") from e
        try:
            keyring.delete_password(self._service, self._username)
        except PasswordDeleteError:
            pass
        except KeyringError as e:
            raise TokenStoreUnavailable() from e


class FileTokenStore:
    name = "file"

    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> str | None:
        try:
            return self._path.read_text()
        except FileNotFoundError:
            return None

    def save(self, tokens: str) -> None:
        parent = self._path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        tmp = parent / f".{self._path.name}.{secrets.token_hex(8)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "w") as f:
                f.write(tokens)
            os.replace(tmp, self._path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        os.chmod(self._path, 0o600)

    def delete(self) -> None:
        try:
            self._path.unlink()
        except FileNotFoundError:
            pass


def default_stores(paths: Paths) -> list[TokenStore]:
    return [KeychainTokenStore(), FileTokenStore(paths.token_file)]


def load_tokens(stores: list[TokenStore]) -> tuple[str, TokenStore] | None:
    for store in stores:
        try:
            tokens = store.load()
        except TokenStoreUnavailable:
            continue
        if tokens is not None:
            return (tokens, store)
    return None


class LoginRateLimited(Exception):
    pass


class LoginFailed(Exception):
    pass


def open_source(
    paths: Paths,
    settings: Settings,
    garmin_factory: Callable = Garmin,
    sleep: Callable[[float], None] = time.sleep,
) -> GarminSource:
    result = load_tokens(default_stores(paths))
    if result is None:
        raise AuthRequired("No stored tokens. Run `trainer login` in a terminal.")
    tokens, store = result
    garmin = garmin_factory()
    try:
        garmin.login(tokenstore=tokens)
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        raise AuthRequired("Garmin session expired. Run `trainer login` in a terminal.")
    except GarminConnectTooManyRequestsError:
        raise RateLimited("Garmin is rate limiting requests. Try again later.")
    current = garmin.client.dumps()
    if current != tokens:
        store.save(current)
    return GarminSource(
        garmin,
        Pacer(settings.request_interval_s, sleep=sleep),
        settings.rate_limit_backoff_s,
        on_tokens_changed=store.save,
        sleep=sleep,
    )


def interactive_login(
    store: TokenStore,
    others: list[TokenStore],
    email: str,
    password: str,
    prompt_mfa: Callable[[], str],
    garmin_factory: Callable = Garmin,
) -> None:
    garmin = garmin_factory(email=email, password=password, prompt_mfa=prompt_mfa)
    try:
        garmin.login()
    except GarminConnectTooManyRequestsError:
        raise LoginRateLimited(
            "Garmin is rate limiting logins. Wait at least an hour before trying again."
        )
    except (GarminConnectAuthenticationError, GarminConnectConnectionError):
        raise LoginFailed() from None
    saved = garmin.client.dumps()
    store.save(saved)
    if store.load() != saved:
        raise TokenStoreUnavailable(f"{store.name}: token save did not round-trip")
    for other in others:
        other.delete()
