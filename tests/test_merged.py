import sqlite3
import subprocess
from pathlib import Path
from typing import Any

import pytest

from factory import cli, config, github, store


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    conn = store.connect()
    for number, state in ((3, "pr_open"), (5, "pr_open"), (2, "awaiting_approval")):
        store.start_issue(conn, number, f"Issue {number}")
        url = f"https://github.com/o/r/pull/{number + 10}"
        store.update_issue(conn, number, state=state, pr_url=url)
    return conn


def test_a_merged_pr_labels_its_issue_merged(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    merged = {"https://github.com/o/r/pull/13": "2026-11-19T20:53:08Z"}  # #5's PR is open
    asked: list[str] = []
    labelled: list[tuple[int, str]] = []

    def merged_at(url: str) -> str | None:
        asked.append(url)
        return merged.get(url)

    monkeypatch.setattr(github, "merged_at", merged_at)
    monkeypatch.setattr(github, "set_status", lambda n, s: labelled.append((n, s)))
    cli.mark_merged()
    assert sorted(asked) == ["https://github.com/o/r/pull/13", "https://github.com/o/r/pull/15"]
    assert labelled == [(3, "merged")]
    rows = {r["number"]: r for r in db.execute("SELECT * FROM issues")}
    assert (rows[3]["state"], rows[3]["merged_at"]) == ("merged", "2026-11-19T20:53:08Z")
    assert (rows[5]["state"], rows[5]["merged_at"]) == ("pr_open", None)

    cli.mark_merged()  # the next tick no longer asks about #3
    assert asked[2:] == ["https://github.com/o/r/pull/15"]


def test_a_failed_label_keeps_the_issue_for_the_next_tick(
    db: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(n: int, s: str) -> None:
        raise subprocess.CalledProcessError(1, ["gh"])

    monkeypatch.setattr(github, "merged_at", lambda url: "2026-11-19T20:53:08Z")
    monkeypatch.setattr(github, "set_status", broken)
    cli.mark_merged()  # logged, not raised
    states = {r["state"] for r in db.execute("SELECT state FROM issues WHERE number IN (3, 5)")}
    assert states == {"pr_open"}


@pytest.mark.parametrize(
    ("output", "expected"),
    [
        ('{"mergedAt": "2026-11-19T20:53:08Z"}', "2026-11-19T20:53:08Z"),
        ('{"mergedAt": null}', None),  # open, or closed without merge
        ('{"mergedAt": ""}', None),
    ],
)
def test_merged_at(monkeypatch: pytest.MonkeyPatch, output: str, expected: str | None) -> None:
    calls: list[list[str]] = []

    def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, output, "")

    monkeypatch.setattr(subprocess, "run", run)
    assert github.merged_at("https://github.com/o/r/pull/5") == expected
    assert calls == [["gh", "pr", "view", "https://github.com/o/r/pull/5", "--json", "mergedAt"]]


def test_an_older_database_gains_merged_at(tmp_path: Path) -> None:
    path = tmp_path / "factory.db"
    old = sqlite3.connect(path)
    old.execute(
        "CREATE TABLE issues (number INTEGER PRIMARY KEY, title TEXT NOT NULL, "
        "state TEXT NOT NULL, session_id TEXT, thread_id TEXT NOT NULL, "
        "plan_comment_id INTEGER, plan_comment_at TEXT, pr_url TEXT, "
        "started_at TEXT NOT NULL, approved_at TEXT, finished_at TEXT)"
    )
    old.execute("INSERT INTO issues VALUES (3, 't', 'pr_open', NULL, 'issue-3', "
                "NULL, NULL, 'u', 's', NULL, NULL)")  # fmt: skip
    old.commit()
    old.close()
    db = store.connect(path)
    row = db.execute("SELECT * FROM issues").fetchone()
    assert (row["state"], row["merged_at"]) == ("pr_open", None)
