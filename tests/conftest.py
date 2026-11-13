import pytest

from trainer.db import connect


class _FakeKeyringBackend:
    def __init__(self) -> None:
        self._store: dict[tuple[str, str], str] = {}

    def get_password(self, service: str, username: str) -> str | None:
        return self._store.get((service, username))

    def set_password(self, service: str, username: str, password: str) -> None:
        self._store[(service, username)] = password

    def delete_password(self, service: str, username: str) -> None:
        self._store.pop((service, username), None)


@pytest.fixture
def memory_keyring():
    return _FakeKeyringBackend()


@pytest.fixture
def db(tmp_path):
    return connect(tmp_path / "t.db")
