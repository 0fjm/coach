from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath


class PlanPathError(ValueError):
    pass


def resolve_plan_path(plans_dir: Path, name: str) -> Path:
    if not name:
        raise PlanPathError("Plan name must not be empty")
    if name.startswith("/") or "\\" in name:
        raise PlanPathError(f"Plan name must be a relative POSIX path: {name!r}")
    if not name.endswith(".md"):
        raise PlanPathError(f"Plan name must end with .md: {name!r}")
    raw_parts = name.split("/")
    if any(seg == "" for seg in raw_parts):
        raise PlanPathError(f"Empty segment in plan name: {name!r}")
    posix = PurePosixPath(name)
    for segment in posix.parts:
        if segment == "" or segment == "." or segment == "..":
            raise PlanPathError(f"Invalid segment in plan name: {name!r}")
        if segment.startswith("."):
            raise PlanPathError(f"Hidden segment in plan name: {name!r}")
    resolved = (plans_dir / name).resolve()
    if not str(resolved).startswith(str(plans_dir.resolve()) + "/") and resolved != plans_dir.resolve():
        raise PlanPathError(f"Plan path escapes plans_dir: {name!r}")
    return resolved


def list_plans(plans_dir: Path) -> list[str]:
    results = []
    for p in sorted(plans_dir.rglob("*.md")):
        rel = p.relative_to(plans_dir)
        parts = rel.parts
        if any(seg.startswith(".") for seg in parts):
            continue
        results.append(rel.as_posix())
    return results


def read_plan(plans_dir: Path, name: str) -> str:
    path = resolve_plan_path(plans_dir, name)
    if not path.exists():
        raise PlanPathError(f"No plan named {name}")
    return path.read_text(encoding="utf-8")


@dataclass(frozen=True)
class WriteResult:
    path: str
    committed: bool
    commit: str | None
    commit_error: str | None


def write_plan(
    repo_root: Path,
    plans_dir: Path,
    name: str,
    content: str,
    summary: str | None,
    settings,
) -> WriteResult:
    encoded = content.encode("utf-8")
    if len(encoded) > settings.plan_max_bytes:
        raise PlanPathError(
            f"Content exceeds max size of {settings.plan_max_bytes} bytes"
        )
    path = resolve_plan_path(plans_dir, name)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not content.endswith("\n"):
        content = content + "\n"
    path.write_text(content, encoding="utf-8")
    rel = path.relative_to(repo_root).as_posix()
    add = subprocess.run(
        ["git", "-C", str(repo_root), "add", "--", rel],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if add.returncode != 0:
        last_line = (add.stderr.strip().splitlines() or ["git add failed"])[-1]
        return WriteResult(path=name, committed=False, commit=None, commit_error=last_line)
    no_change = subprocess.run(
        ["git", "-C", str(repo_root), "diff", "--cached", "--quiet", "--", rel],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if no_change.returncode == 0:
        return WriteResult(path=name, committed=False, commit=None, commit_error=None)
    msg = f"plan: {name}"
    commit_args = ["git", "-C", str(repo_root), "commit", "--only", "-m", msg]
    if summary:
        commit_args += ["-m", summary]
    commit_args += ["--", rel]
    result = subprocess.run(
        commit_args,
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    if result.returncode != 0:
        last_line = (result.stderr.strip().splitlines() or ["git commit failed"])[-1]
        return WriteResult(path=name, committed=False, commit=None, commit_error=last_line)
    sha_result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
    )
    sha = sha_result.stdout.strip() if sha_result.returncode == 0 else None
    return WriteResult(path=name, committed=True, commit=sha, commit_error=None)
