import subprocess

import pytest

from trainer.config import Settings
from trainer.plans import (
    PlanPathError,
    WriteResult,
    list_plans,
    read_plan,
    resolve_plan_path,
    write_plan,
)


@pytest.fixture
def plans_dir(tmp_path):
    d = tmp_path / "plans"
    d.mkdir()
    return d


def git(repo, *args) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


@pytest.fixture
def git_repo(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "test@example.com")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "commit.gpgsign")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "false")
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    (tmp_path / "README.md").write_text("init\n")
    subprocess.run(
        ["git", "-C", str(tmp_path), "add", "README.md"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp_path), "commit", "-m", "init"],
        check=True,
        capture_output=True,
    )
    return tmp_path


@pytest.mark.parametrize(
    "name",
    [
        "../x.md",
        "/etc/passwd.md",
        "a/../../x.md",
        "x.txt",
        ".hidden.md",
        "weeks/.x.md",
        "",
        "weeks//x.md",
    ],
)
def test_resolve_rejects(plans_dir, name):
    with pytest.raises(PlanPathError):
        resolve_plan_path(plans_dir, name)


def test_resolve_rejects_symlink_escape(plans_dir, tmp_path):
    (plans_dir / "out").symlink_to(tmp_path)
    with pytest.raises(PlanPathError):
        resolve_plan_path(plans_dir, "out/x.md")


def test_list_plans(plans_dir):
    (plans_dir / "weeks").mkdir()
    (plans_dir / "weeks" / "2026-W46.md").write_text("w\n")
    (plans_dir / "profile.md").write_text("p\n")
    (plans_dir / "notes.txt").write_text("n\n")
    assert list_plans(plans_dir) == ["profile.md", "weeks/2026-W46.md"]


def test_read_plan(plans_dir):
    (plans_dir / "log.md").write_text("content\n")
    assert read_plan(plans_dir, "log.md") == "content\n"


def test_read_plan_missing(plans_dir):
    with pytest.raises(PlanPathError, match="No plan named missing.md"):
        read_plan(plans_dir, "missing.md")


def test_write_commits_only_that_file(git_repo):
    (git_repo / "other.txt").write_text("x\n")
    git(git_repo, "add", "other.txt")
    r = write_plan(
        git_repo, git_repo / "plans", "weeks/2026-W46.md", "# W46", None, Settings()
    )
    assert r.committed
    assert git(git_repo, "show", "--name-only", "--format=", "HEAD").split() == [
        "plans/weeks/2026-W46.md"
    ]
    assert "other.txt" in git(git_repo, "diff", "--cached", "--name-only")


def test_write_unchanged_content_makes_no_commit(git_repo):
    write_plan(git_repo, git_repo / "plans", "log.md", "a", None, Settings())
    head = git(git_repo, "rev-parse", "HEAD")
    assert (
        write_plan(git_repo, git_repo / "plans", "log.md", "a", None, Settings()).committed
        is False
    )
    assert git(git_repo, "rev-parse", "HEAD") == head


def test_write_reports_commit_failure_but_keeps_file(tmp_path):
    r = write_plan(tmp_path, tmp_path / "plans", "log.md", "a", None, Settings())
    assert (tmp_path / "plans" / "log.md").read_text() == "a\n"
    assert r.committed is False and r.commit_error


def test_write_rejects_oversized(git_repo):
    with pytest.raises(PlanPathError):
        write_plan(
            git_repo,
            git_repo / "plans",
            "big.md",
            "x" * 300_000,
            None,
            Settings(),
        )


def test_write_result_is_frozen():
    r = WriteResult(path="log.md", committed=True, commit="abc", commit_error=None)
    with pytest.raises(Exception):
        r.path = "other.md"


def test_write_with_summary(git_repo):
    r = write_plan(
        git_repo, git_repo / "plans", "log.md", "content", "My summary", Settings()
    )
    assert r.committed
    msg = git(git_repo, "log", "--format=%B", "-1")
    assert "My summary" in msg


def test_write_result_commit_sha(git_repo):
    r = write_plan(git_repo, git_repo / "plans", "log.md", "content", None, Settings())
    assert r.committed
    assert r.commit == git(git_repo, "rev-parse", "HEAD")


def test_git_never_reads_server_stdin(git_repo, monkeypatch):
    real_run = subprocess.run
    stdins = []

    def recording_run(*args, **kwargs):
        stdins.append(kwargs.get("stdin"))
        return real_run(*args, **kwargs)

    monkeypatch.setattr(subprocess, "run", recording_run)
    write_plan(git_repo, git_repo / "plans", "season.md", "# Season", None, Settings())
    assert len(stdins) == 4 and all(s is subprocess.DEVNULL for s in stdins)
