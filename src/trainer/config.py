import os
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
HOME_POINTER = PROJECT_ROOT / ".trainer-home"


@dataclass(frozen=True)
class Paths:
    root: Path
    db: Path
    plans: Path
    secrets: Path
    token_file: Path
    lock: Path


@dataclass(frozen=True)
class Settings:
    request_interval_s: float = 1.0
    overlap_days: int = 7
    gap_threshold_days: int = 14
    sql_default_rows: int = 200
    sql_max_rows: int = 500
    sql_timeout_s: float = 5.0
    rate_limit_backoff_s: tuple[float, ...] = (60.0, 120.0, 240.0)
    backfill_default_days: int = 365
    plan_max_bytes: int = 262144


def load_paths(root: Path | None = None) -> Paths:
    if root is None:
        env = os.environ.get("TRAINER_HOME")
        if env:
            root = Path(env)
        elif HOME_POINTER.exists():
            root = Path(HOME_POINTER.read_text(encoding="utf-8").strip())
        else:
            root = PROJECT_ROOT
    return Paths(
        root=root,
        db=root / "data" / "trainer.db",
        plans=root / "plans",
        secrets=root / ".secrets",
        token_file=root / ".secrets" / "garmin.token",
        lock=root / "data" / "sync.lock",
    )


def load_settings() -> Settings:
    interval = os.environ.get("TRAINER_REQUEST_INTERVAL")
    if interval is not None:
        return Settings(request_interval_s=float(interval))
    return Settings()
