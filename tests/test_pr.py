import sqlite3
import subprocess
from pathlib import Path

import pytest

from factory import config, github, pr


def git(*args: str, cwd: Path) -> str:
    out = subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)
    return out.stdout.strip()


@pytest.fixture(autouse=True)
def identity(monkeypatch: pytest.MonkeyPatch) -> None:
    for role in ("AUTHOR", "COMMITTER"):
        monkeypatch.setenv(f"GIT_{role}_NAME", "t")
        monkeypatch.setenv(f"GIT_{role}_EMAIL", "t@t")


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A bare repo as `origin`, with one commit on main."""
    seed = tmp_path / "seed"
    seed.mkdir()
    git("init", "-q", "-b", "main", cwd=seed)
    (seed / "app.py").write_text("print('hi')\n")
    git("add", "-A", cwd=seed)
    git("commit", "-q", "-m", "init", cwd=seed)
    bare = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(seed), str(bare), cwd=tmp_path)
    return bare


def workspace(origin: Path, tmp_path: Path, files: dict[str, str]) -> tuple[str, bytes]:
    """A workspace patch made like the VM makes it: untracked files included, binary."""
    work = tmp_path / "work"
    git("clone", "-q", str(origin), str(work), cwd=tmp_path)
    base = git("rev-parse", "HEAD", cwd=work)
    for name, text in files.items():
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(text)
    git("-c", "core.excludesFile=/dev/null", "add", "-A", cwd=work)
    patch = subprocess.run(
        ["git", "diff", "--cached", "--binary", base], cwd=work, check=True, capture_output=True
    ).stdout
    return base, patch


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> list[tuple[tuple[str, ...], str | None]]:
    calls: list[tuple[tuple[str, ...], str | None]] = []

    def fake(*args: str, stdin: str | None = None) -> str:
        calls.append((args, stdin))
        return "https://github.com/o/r/pull/9\n"

    monkeypatch.setattr(config, "REPO", "o/r")
    monkeypatch.setattr(github, "_gh", fake)
    return calls


CODE = {
    "app.py": "print('hello')\n",
    "thoughts/shared/plans/plan.md": "# Plan\n",
    "thoughts/shared/validation/report.md": "# Report\n",
}


def test_open_pr_pushes_code_only_and_opens_the_pr(
    origin: Path, tmp_path: Path, gh: list[tuple[tuple[str, ...], str | None]]
) -> None:
    base, patch = workspace(origin, tmp_path, CODE)
    message = pr.commit_message(3, "CSV export misses the header row", ["bug"])
    body = pr.pr_body(3, "Plan summary.", "All checks pass.", "| table |")
    url = pr.open_pr(3, message, body, base, patch, repo=str(origin))

    assert url == "https://github.com/o/r/pull/9"
    files = git("ls-tree", "-r", "--name-only", "factory/issue-3", cwd=origin).splitlines()
    assert files == ["app.py"]  # thoughts/ stripped
    assert git("rev-parse", "factory/issue-3~1", cwd=origin) == base
    assert git("log", "-1", "--format=%B", "factory/issue-3", cwd=origin) == message
    assert git("log", "-1", "--format=%(trailers)", "factory/issue-3", cwd=origin) == ""
    args, stdin = gh[0]
    assert args == (
        "pr", "create", "-R", "o/r", "--head", "factory/issue-3", "--title", message,
        "--body-file", "-",
    )  # fmt: skip
    assert stdin is not None and stdin.startswith("Closes #3\n")


def test_open_pr_refuses_a_workflow_change(
    origin: Path, tmp_path: Path, gh: list[tuple[tuple[str, ...], str | None]]
) -> None:
    base, patch = workspace(origin, tmp_path, {**CODE, ".github/workflows/ci.yml": "on: push\n"})
    with pytest.raises(pr.PrRefused, match=r"\.github/workflows/ci\.yml"):
        pr.open_pr(3, "fix: x (#3)", "Closes #3", base, patch, repo=str(origin))
    assert "factory/issue-3" not in git("branch", "--list", cwd=origin)
    assert gh == []


def test_open_pr_refuses_a_workspace_with_only_documents(
    origin: Path, tmp_path: Path, gh: list[tuple[tuple[str, ...], str | None]]
) -> None:
    docs = {k: v for k, v in CODE.items() if k.startswith("thoughts/")}
    base, patch = workspace(origin, tmp_path, docs)
    with pytest.raises(pr.PrRefused, match="no code changes"):
        pr.open_pr(3, "fix: x (#3)", "Closes #3", base, patch, repo=str(origin))
    assert gh == []


def test_commit_message() -> None:
    assert pr.commit_message(3, "CSV export misses the header row", ["bug"]) == (
        "fix: CSV export misses the header row (#3)"
    )
    assert pr.commit_message(2, "Add a filter", ["factory", "enhancement"]).startswith("feat: ")
    long = pr.commit_message(1, "x" * 59 + " and then some more words", [])
    assert long == f"fix: {'x' * 59} (#1)"


def test_metrics_table_has_a_row_per_stage_and_a_total() -> None:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    rows = db.execute(
        "SELECT 'research' stage, 'ok' status, 0.02 cost_usd, 40 tool_calls, "
        "30 subagent_tool_calls, 12 num_turns, 61.4 wall_s "
        "UNION ALL SELECT 'plan', 'ok', 0.03, 20, 10, 8, 30.0"
    ).fetchall()
    table = pr.metrics_table(rows)
    assert "| research | ok | $0.0200 | 40 (30) | 12 | 61 s |" in table
    assert "| **total** | | **$0.0500** | **60 (40)** | | **91 s** |" in table
