from __future__ import annotations

import json
import os
from datetime import date

import pytest
from garminconnect import GarminConnectAuthenticationError, GarminConnectConnectionError

from fakes import FakeGarmin
import trainer.cli as cli
from trainer.sync import SyncResult


def ok_result() -> SyncResult:
    return SyncResult(
        status="ok",
        start=date(2024, 1, 1),
        end=date(2024, 1, 7),
        requests=0,
        fetched={},
        new_activities=0,
        last_sync_at=None,
        last_synced_day=None,
        message="Sync complete.",
    )


def result(status: str, message: str) -> SyncResult:
    return SyncResult(
        status=status,
        start=None,
        end=None,
        requests=0,
        fetched={},
        new_activities=0,
        last_sync_at=None,
        last_synced_day=None,
        message=message,
    )


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("TRAINER_HOME", str(tmp_path))
    return tmp_path


def test_backfill_passes_since_and_interval(monkeypatch, home):
    seen = {}
    monkeypatch.setattr(cli, "run_backfill", lambda *a, **k: seen.update(k) or ok_result())
    assert cli.main(["backfill", "--since", "2020-01-01", "--interval", "2"]) == 0
    assert seen["since"] == date(2020, 1, 1) and seen["settings"].request_interval_s == 2.0


def test_sync_nonzero_exit_on_auth_required(monkeypatch, home, capsys):
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: result("auth_required", "Run `trainer login` in a terminal."))
    assert cli.main(["sync"]) == 1
    assert "trainer login" in capsys.readouterr().out


def test_login_reads_password_with_getpass_and_never_prints_it(monkeypatch, home, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "a@b.c")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "s3cret")
    calls = []
    monkeypatch.setattr(cli, "interactive_login", lambda store, others, email, pw, mfa: calls.append((store.name, email, pw)))
    assert cli.main(["login", "--store", "file"]) == 0
    assert calls == [("file", "a@b.c", "s3cret")]
    assert "s3cret" not in capsys.readouterr().out


def test_status_prints_freshness(home, capsys):
    assert cli.main(["status"]) == 0
    assert "Last sync" in capsys.readouterr().out


def test_login_keychain_store_by_default(monkeypatch, home, capsys):
    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "pass")
    calls = []
    monkeypatch.setattr(cli, "interactive_login", lambda store, others, email, pw, mfa: calls.append(store.name))
    assert cli.main(["login"]) == 0
    assert calls == ["keychain"]


def test_login_token_store_unavailable_returns_1(monkeypatch, home, capsys):
    from trainer.auth import TokenStoreUnavailable

    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "pass")
    monkeypatch.setattr(cli, "interactive_login", lambda *a, **k: (_ for _ in ()).throw(TokenStoreUnavailable("SomeBackend")))
    assert cli.main(["login"]) == 1
    out = capsys.readouterr().out
    assert "Could not save the token to the keychain store: TokenStoreUnavailable." in out
    assert "--store file" in out


def test_login_file_store_unavailable_does_not_suggest_file(monkeypatch, home, capsys):
    from trainer.auth import TokenStoreUnavailable

    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "pass")
    monkeypatch.setattr(cli, "interactive_login", lambda *a, **k: (_ for _ in ()).throw(TokenStoreUnavailable("x")))
    assert cli.main(["login", "--store", "file"]) == 1
    out = capsys.readouterr().out
    assert "Could not save the token to the file store: TokenStoreUnavailable." in out
    assert "--store" not in out


def test_login_rate_limited_returns_1(monkeypatch, home, capsys):
    from trainer.auth import LoginRateLimited

    monkeypatch.setattr("builtins.input", lambda prompt: "user@example.com")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "pass")
    monkeypatch.setattr(cli, "interactive_login", lambda *a, **k: (_ for _ in ()).throw(LoginRateLimited("Too many requests.")))
    assert cli.main(["login"]) == 1
    assert "Too many requests" in capsys.readouterr().out


def test_sync_ok_exit_0(monkeypatch, home, capsys):
    monkeypatch.setattr(cli, "run_sync", lambda *a, **k: ok_result())
    assert cli.main(["sync"]) == 0
    assert "Sync complete" in capsys.readouterr().out


def test_backfill_no_since(monkeypatch, home):
    seen = {}
    monkeypatch.setattr(cli, "run_backfill", lambda *a, **k: seen.update(k) or ok_result())
    assert cli.main(["backfill"]) == 0
    assert "since" not in seen or seen.get("since") is None


def test_rebuild_prints_rows(monkeypatch, home, capsys):
    monkeypatch.setattr(cli, "rebuild", lambda conn: 42)
    assert cli.main(["rebuild"]) == 0
    assert "42" in capsys.readouterr().out


def test_status_on_empty_db(home, capsys):
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "Last sync:" in out
    assert "Last synced day:" in out
    assert "Oldest synced day:" in out
    assert "Activities:" in out
    assert "never" in out
    assert "none" in out


@pytest.mark.parametrize("error", [GarminConnectAuthenticationError, GarminConnectConnectionError])
def test_login_failure_prints_hint_without_secrets(monkeypatch, home, capsys, error):
    monkeypatch.setattr("builtins.input", lambda prompt: "a@b.c")
    monkeypatch.setattr(cli.getpass, "getpass", lambda prompt: "s3cret")

    class FailingGarmin(FakeGarmin):
        def __init__(self, **kwargs):
            super().__init__()

        def login(self, **kwargs):
            raise error("bad credentials for a@b.c with s3cret")

    real_login = cli.interactive_login
    monkeypatch.setattr(
        cli, "interactive_login",
        lambda *a, **k: real_login(*a, **k, garmin_factory=FailingGarmin),
    )
    assert cli.main(["login", "--store", "file"]) == 1
    out = capsys.readouterr().out
    assert out.strip() == "Garmin login failed. Check email and password, then wait a few minutes before trying again."
    assert "s3cret" not in out


def test_rebuild_is_busy_while_sync_holds_the_lock(monkeypatch, home, capsys):
    from trainer.config import load_paths
    from trainer.sync import sync_lock

    monkeypatch.setattr(cli, "rebuild", lambda conn: pytest.fail("rebuild ran while locked"))
    with sync_lock(load_paths().lock):
        assert cli.main(["rebuild"]) == 1
    assert "already running" in capsys.readouterr().out


def test_init_creates_folders_and_mcp_config(monkeypatch, tmp_path, capsys):
    data = tmp_path / "data-home"
    project = tmp_path / "project"
    project.mkdir()
    pointer = project / ".trainer-home"
    monkeypatch.setattr(cli, "PROJECT_ROOT", project)
    monkeypatch.setattr(cli, "HOME_POINTER", pointer)
    assert cli.main(["init", "--home", str(data)]) == 0
    assert pointer.read_text().strip() == str(data.resolve())
    assert (data / "plans" / "weeks").is_dir()
    assert (data / "data").is_dir()
    assert (data / ".secrets").is_dir()
    mcp = json.loads((project / ".mcp.json").read_text())
    server = mcp["mcpServers"]["trainer"]
    assert server["env"]["TRAINER_HOME"] == str(data.resolve())
    assert str(project) in server["args"]
    assert "/coach" in capsys.readouterr().out


def test_init_keeps_existing_mcp_config(monkeypatch, tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    (project / ".mcp.json").write_text("{}")
    monkeypatch.setattr(cli, "PROJECT_ROOT", project)
    monkeypatch.setattr(cli, "HOME_POINTER", project / ".trainer-home")
    assert cli.main(["init", "--home", str(tmp_path / "data-home")]) == 0
    assert (project / ".mcp.json").read_text() == "{}"
