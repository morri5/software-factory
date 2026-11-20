import sqlite3
from pathlib import Path

import pytest

from factory import config, report, store


def issue(db: sqlite3.Connection, number: int, state: str, **fields: object) -> None:
    store.start_issue(db, number, f"Issue {number}")
    store.update_issue(db, number, state=state, started_at="2026-11-19T20:00:00+00:00", **fields)


def stage(db: sqlite3.Connection, number: int, name: str, cost: float, at: str) -> None:
    db.execute(
        """INSERT INTO stage_runs (issue, stage, session_id, run_id, status, input_tokens,
            cache_write_tokens, cache_read_tokens, output_tokens, cost_usd, tool_calls,
            subagent_tool_calls, num_turns, wall_s, commands_sha, started_at, finished_at)
        VALUES (?, ?, 's', 'r', 'ok', 1, 0, 0, 1, ?, 10, 6, 3, 60.0, 'c', ?, ?)""",
        (number, name, cost, at, at),
    )
    db.commit()


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> sqlite3.Connection:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    monkeypatch.setattr(config, "REPO", "o/r")
    conn = store.connect()
    # merged: 10 min in all, of which 4 min waiting for approval
    issue(
        conn, 3, "merged",
        plan_comment_at="2026-11-19T20:03:00Z", approved_at="2026-11-19T20:07:00Z",
        finished_at="2026-11-19T20:10:00+00:00", pr_url="https://github.com/o/r/pull/5",
    )  # fmt: skip
    # waiting for approval: factory time up to the plan
    issue(conn, 4, "awaiting_approval", plan_comment_at="2026-11-19T20:02:00Z")
    # failed in research, no approval
    issue(conn, 1, "failed", finished_at="2026-11-19T20:01:30+00:00")
    stage(conn, 3, "research", 0.05, "2026-11-19T19:00:00+00:00")  # an earlier run: left out
    for name, cost in (("research", 0.05), ("plan", 0.03), ("implement", 0.03)):
        stage(conn, 3, name, cost, "2026-11-19T20:01:00+00:00")
    stage(conn, 4, "research", 0.04, "2026-11-19T20:01:00+00:00")
    stage(conn, 4, "plan", 0.02, "2026-11-19T20:01:30+00:00")
    stage(conn, 1, "research", 0.01, "2026-11-19T20:01:00+00:00")
    return conn


def test_report_rows_and_totals(db: sqlite3.Connection) -> None:
    text = report.render(db, "2026-11-20")
    assert "| #1 Issue 1 | failed |  | $0.0100 | 10 (6) | 1 min 30 s | n/a |" in text
    assert (
        "| #3 Issue 3 | merged | [#5](https://github.com/o/r/pull/5) | $0.1100 | 30 (18) "
        "| 6 min 00 s | 4 min 00 s |"
    ) in text
    assert "| #4 Issue 4 | awaiting_approval |  | $0.0600 | 20 (12) | 2 min 00 s | n/a |" in text
    assert "- Success rate (PR opened or merged): 1 of 3 (33%)" in text
    assert "- Median cost per issue: $0.0600" in text
    assert "- Median factory time: 2 min 00 s" in text
    # per issue, the stage table of the current run only
    section = text.split("## #3 Issue 3")[1]
    assert "| research | ok | $0.0500 | 10 (6) | 3 | 60 s |" in section
    assert "| **total** | | **$0.1100** | **30 (18)** | | **180 s** |" in section


def test_write_creates_the_file(db: sqlite3.Connection, tmp_path: Path) -> None:
    path = tmp_path / "docs" / "runs.md"
    report.write(path)
    assert path.read_text().startswith("# Factory runs\n")


def test_duration() -> None:
    assert report.duration(None) == "n/a"
    assert report.duration(54.4) == "54 s"
    assert report.duration(282) == "4 min 42 s"
    assert report.duration(3900) == "1 h 05 min"
