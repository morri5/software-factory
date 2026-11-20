"""The factory's own record in SQLite (`var/factory.db`): issues and stage runs."""

import datetime
import sqlite3
from pathlib import Path

from factory import config
from factory.sandbox import StageRun

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    number INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    state TEXT NOT NULL,
    session_id TEXT,
    thread_id TEXT NOT NULL,
    plan_comment_id INTEGER,
    plan_comment_at TEXT,
    pr_url TEXT,
    started_at TEXT NOT NULL,
    approved_at TEXT,
    finished_at TEXT,
    merged_at TEXT
);
CREATE TABLE IF NOT EXISTS stage_runs (
    id INTEGER PRIMARY KEY,
    issue INTEGER NOT NULL REFERENCES issues(number),
    stage TEXT NOT NULL,
    session_id TEXT NOT NULL,
    run_id TEXT NOT NULL,
    status TEXT NOT NULL,
    reason TEXT,
    input_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_read_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    cost_usd REAL NOT NULL,
    tool_calls INTEGER NOT NULL,
    subagent_tool_calls INTEGER NOT NULL,
    num_turns INTEGER,
    wall_s REAL NOT NULL,
    commands_sha TEXT NOT NULL,
    agents_sha TEXT,
    started_at TEXT NOT NULL,
    finished_at TEXT NOT NULL
);
"""


def now() -> str:
    return datetime.datetime.now(datetime.UTC).isoformat(timespec="seconds")


def connect(path: Path | None = None) -> sqlite3.Connection:
    path = path or config.VAR_DIR / "factory.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    columns = {r["name"] for r in db.execute("PRAGMA table_info(issues)")}
    if "merged_at" not in columns:  # a database from before `factory:merged`
        db.execute("ALTER TABLE issues ADD COLUMN merged_at TEXT")
    return db


def start_issue(db: sqlite3.Connection, number: int, title: str) -> None:
    """A new run of the issue: its row starts over (the stage runs are kept)."""
    with db:
        db.execute(
            """
            INSERT INTO issues (number, title, state, thread_id, started_at)
            VALUES (?, ?, 'started', ?, ?)
            ON CONFLICT (number) DO UPDATE SET
                title = excluded.title, state = 'started', session_id = NULL,
                plan_comment_id = NULL, plan_comment_at = NULL, pr_url = NULL,
                started_at = excluded.started_at, approved_at = NULL, finished_at = NULL,
                merged_at = NULL
            """,
            (number, title, f"issue-{number}", now()),
        )


def update_issue(db: sqlite3.Connection, number: int, **fields: object) -> None:
    columns = ", ".join(f"{name} = ?" for name in fields)  # names come from our code only
    with db:
        db.execute(f"UPDATE issues SET {columns} WHERE number = ?", (*fields.values(), number))


def record_stage_run(
    db: sqlite3.Connection,
    issue: int,
    stage: str,
    run: StageRun,
    commands_sha: str,
    started_at: str,
    status: str | None = None,
    reason: str | None = None,
) -> None:
    """One stage run; `status`/`reason` override the run's own (e.g. no FACTORY_RESULT)."""
    u = run.usage
    with db:
        db.execute(
            """
            INSERT INTO stage_runs (issue, stage, session_id, run_id, status, reason,
                input_tokens, cache_write_tokens, cache_read_tokens, output_tokens, cost_usd,
                tool_calls, subagent_tool_calls, num_turns, wall_s, commands_sha, agents_sha,
                started_at, finished_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                issue, stage, str(run.session_id), run.run_id, status or run.status,
                reason if status else run.reason,
                u.get("input_tokens", 0), u.get("cache_creation_input_tokens", 0),
                u.get("cache_read_input_tokens", 0), u.get("output_tokens", 0), run.cost_usd,
                run.tool_calls, run.subagent_tool_calls, run.num_turns, run.wall_s,
                commands_sha, run.agents_sha, started_at, now(),
            ),
        )  # fmt: skip


def current_runs(db: sqlite3.Connection, number: int) -> list[sqlite3.Row]:
    """The stage runs of the issue's current run (since its row last started over)."""
    return db.execute(
        """
        SELECT r.* FROM stage_runs r JOIN issues i ON i.number = r.issue
        WHERE r.issue = ? AND r.started_at >= i.started_at ORDER BY r.id
        """,
        (number,),
    ).fetchall()
