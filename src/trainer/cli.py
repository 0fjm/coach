from __future__ import annotations

import argparse
import getpass
import json
import subprocess
import sys
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

from trainer.auth import (
    FileTokenStore,
    KeychainTokenStore,
    LoginFailed,
    LoginRateLimited,
    TokenStoreUnavailable,
    default_stores,
    interactive_login,
    open_source,
)
from trainer.config import HOME_POINTER, PROJECT_ROOT, load_paths, load_settings
from trainer.db import connect
from trainer.normalize import rebuild
from trainer.sync import (
    SyncBusy,
    SyncResult,
    last_sync_at,
    last_synced_day,
    run_backfill,
    run_sync,
    sync_lock,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="trainer")
    sub = parser.add_subparsers(dest="command")

    init_p = sub.add_parser("init")
    init_p.add_argument("--home", type=Path, default=None)

    login_p = sub.add_parser("login")
    login_p.add_argument("--store", choices=["keychain", "file"], default="keychain")

    sub.add_parser("sync")

    backfill_p = sub.add_parser("backfill")
    backfill_p.add_argument("--since", type=date.fromisoformat, default=None)
    backfill_p.add_argument("--interval", type=float, default=None)

    sub.add_parser("rebuild")
    sub.add_parser("status")

    return parser


def _inside_git_work_tree(path: Path) -> bool:
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "--is-inside-work-tree"],
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
    )
    return result.returncode == 0


def _cmd_init(args: argparse.Namespace) -> int:
    root = args.home.expanduser().resolve() if args.home else None
    paths = load_paths(root)
    (paths.plans / "weeks").mkdir(parents=True, exist_ok=True)
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    paths.secrets.mkdir(mode=0o700, exist_ok=True)
    if root is not None:
        HOME_POINTER.write_text(f"{paths.root}\n", encoding="utf-8")
    print(f"Data folder: {paths.root}")

    if not _inside_git_work_tree(paths.root):
        subprocess.run(
            ["git", "-C", str(paths.root), "init", "-q"],
            check=False,
            stdin=subprocess.DEVNULL,
        )
        (paths.root / ".gitignore").write_text("data/\n.secrets/\n", encoding="utf-8")

    mcp_file = PROJECT_ROOT / ".mcp.json"
    if mcp_file.exists():
        print(f"Kept existing {mcp_file}. Check that TRAINER_HOME is {paths.root}.")
    else:
        config = {
            "mcpServers": {
                "trainer": {
                    "command": "uv",
                    "args": ["run", "--directory", str(PROJECT_ROOT), "--frozen", "trainer-mcp"],
                    "env": {"TRAINER_HOME": str(paths.root)},
                }
            }
        }
        mcp_file.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
        print(f"Wrote {mcp_file}")

    print("Next steps:")
    print("  1. Start Claude Code in this repo and run /coach. It asks a few questions and writes your profile.")
    print("  2. Run `uv run trainer login` in your own terminal.")
    print("  3. Run `uv run trainer backfill` in your own terminal.")
    return 0


def _cmd_login(args: argparse.Namespace) -> int:
    paths = load_paths()
    stores = default_stores(paths)
    if args.store == "keychain":
        store = stores[0]
        others = stores[1:]
    else:
        store = stores[1]
        others = stores[:1]

    email = input("Garmin email: ")
    password = getpass.getpass("Garmin password: ")

    def prompt_mfa() -> str:
        return input("MFA code: ")

    try:
        interactive_login(store, others, email, password, prompt_mfa)
    except TokenStoreUnavailable as exc:
        print(f"Could not save the token to the {store.name} store: {type(exc).__name__}.")
        if store.name == "keychain":
            print("Rerun with `trainer login --store file`.")
        return 1
    except LoginRateLimited as exc:
        print(str(exc))
        return 1
    except LoginFailed:
        print("Garmin login failed. Check email and password, then wait a few minutes before trying again.")
        return 1
    return 0


def _cmd_sync(_args: argparse.Namespace) -> int:
    paths = load_paths()
    settings = load_settings()
    conn = connect(paths.db)
    result: SyncResult = run_sync(
        conn=conn,
        source_factory=lambda: open_source(paths, settings),
        lock_path=paths.lock,
        today=date.today(),
        now=datetime.now(UTC),
        settings=settings,
        on_progress=print,
    )
    print(result.message)
    return 0 if result.status == "ok" else 1


def _cmd_backfill(args: argparse.Namespace) -> int:
    paths = load_paths()
    settings = load_settings()
    if args.interval is not None:
        settings = replace(settings, request_interval_s=args.interval)
    conn = connect(paths.db)
    result: SyncResult = run_backfill(
        conn=conn,
        source_factory=lambda: open_source(paths, settings),
        lock_path=paths.lock,
        today=date.today(),
        now=datetime.now(UTC),
        settings=settings,
        since=args.since,
        on_progress=print,
    )
    print(result.message)
    return 0 if result.status == "ok" else 1


def _cmd_rebuild(_args: argparse.Namespace) -> int:
    paths = load_paths()
    conn = connect(paths.db)
    try:
        with sync_lock(paths.lock):
            rows = rebuild(conn)
    except SyncBusy as exc:
        print(str(exc))
        return 1
    print(f"Replayed {rows} rows.")
    return 0


def _cmd_status(_args: argparse.Namespace) -> int:
    paths = load_paths()
    conn = connect(paths.db)

    last_sync = last_sync_at(conn)
    last_day = last_synced_day(conn)

    oldest_row = conn.execute(
        "SELECT MIN(key) FROM fetch_status"
        " WHERE endpoint = 'user_summary' AND status IN ('ok', 'empty')"
    ).fetchone()
    oldest_day = oldest_row[0] if oldest_row and oldest_row[0] else None

    activity_count = conn.execute("SELECT COUNT(*) FROM activities").fetchone()[0]

    print(f"Last sync: {last_sync or 'never'}")
    print(f"Last synced day: {last_day.isoformat() if last_day else 'none'}")
    print(f"Oldest synced day: {oldest_day or 'none'}")
    print(f"Activities: {activity_count}")
    return 0


_COMMANDS = {
    "init": _cmd_init,
    "login": _cmd_login,
    "sync": _cmd_sync,
    "backfill": _cmd_backfill,
    "rebuild": _cmd_rebuild,
    "status": _cmd_status,
}


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 1
    return _COMMANDS[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
