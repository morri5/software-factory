"""`factory run --issue N` (one issue, in the foreground), `factory watch` (the loop) and
`factory report` (`docs/runs.md` from the factory's record).

Both talk to a running session API (`make -C ../sandboxed-agents-lab api`); the
factory never starts its own, since only one API process may run per database.
"""

import argparse
import asyncio
import logging
import sys
import time
from typing import Any
from uuid import UUID

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from factory import config, github, graph, report, sandbox, store

log = logging.getLogger("factory")


async def run_issue(number: int, decision: github.Decision | None = None) -> dict[str, Any]:
    """Run the issue's graph until it ends or waits for approval.

    Without a decision the issue starts over; with one, its thread resumes at the
    approval. At the end the issue's sandbox session is closed.
    """
    db = store.connect()
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(config.VAR_DIR / "checkpoints.db")) as saver:
        app = graph.build(saver)
        thread = {"configurable": {"thread_id": f"issue-{number}"}}
        start: dict[str, Any] | Command[Any]
        if decision is None:
            issue = github.issue(number)
            store.start_issue(db, number, issue["title"])
            start = {"issue": number, "title": issue["title"], "body": issue["body"] or ""}
        else:
            if (await app.aget_state(thread)).next != ("approve",):
                raise RuntimeError(f"issue #{number} has no thread waiting for approval")
            start = Command(resume=decision)
        state: dict[str, Any] = await app.ainvoke(start, thread)
        if not (await app.aget_state(thread)).next:  # reached END
            if state.get("session_id"):
                await sandbox.close_session(UUID(state["session_id"]))
            store.update_issue(db, number, finished_at=store.now())
    return state


def run_once(number: int, decision: github.Decision | None = None) -> bool:
    """Run or resume one issue; True if it went well. An unexpected error fails the issue."""
    try:
        state = asyncio.run(run_issue(number, decision))
    except Exception as exc:
        log.exception("issue #%d: the factory failed", number)
        try:
            db = store.connect()
            store.update_issue(db, number, state="failed", finished_at=store.now())
            github.comment(number, f"**Factory failed.** Unexpected error: `{exc!r}`"[:2000])
            github.set_status(number, "failed")
            row = db.execute("SELECT session_id FROM issues WHERE number = ?", (number,)).fetchone()
            if row and row["session_id"]:
                asyncio.run(sandbox.close_session(UUID(row["session_id"])))
        except Exception:
            log.exception("issue #%d: could not report the failure", number)
        return False
    waiting = " (awaiting approval)" if "__interrupt__" in state else ""
    log.info("issue #%d: %s%s", number, state.get("outcome"), waiting)
    return state.get("outcome") == "ok"


def resume_decided() -> None:
    """Resume every issue whose plan the approver has answered since the last tick."""
    waiting = store.connect().execute(
        "SELECT number, plan_comment_at FROM issues WHERE state = 'awaiting_approval'"
    )
    for row in waiting.fetchall():
        try:
            decision = github.find_decision(row["number"], row["plan_comment_at"], config.APPROVER)
        except Exception:
            log.exception("issue #%d: could not read the comments", row["number"])
            continue
        if decision is not None:
            log.info("issue #%d: %s by %s", row["number"], decision["action"], config.APPROVER)
            run_once(row["number"], decision)


def mark_merged() -> None:
    """Label `factory:merged` the issues whose PR was merged since the last tick.

    The list comes from SQLite: a merged PR closes its issue, and the queue lists open
    issues only. A PR closed without merge changes nothing.
    """
    db = store.connect()
    for row in db.execute("SELECT number, pr_url FROM issues WHERE state = 'pr_open'").fetchall():
        try:
            merged = github.merged_at(row["pr_url"])
            if merged is not None:
                github.set_status(row["number"], "merged")
                store.update_issue(db, row["number"], state="merged", merged_at=merged)
                log.info("issue #%d: PR merged", row["number"])
        except Exception:
            log.exception("issue #%d: could not check its PR", row["number"])


def watch() -> None:
    log.info("watching %s every %d s", config.REPO, config.POLL_S)
    if created := github.ensure_labels():
        log.info("created labels: %s", ", ".join(created))
    while True:
        resume_decided()
        mark_merged()
        try:
            queued = github.queued_issues()
        except Exception:
            log.exception("could not list the issues")
            queued = []
        if queued:
            run_once(queued[0]["number"])
        time.sleep(config.POLL_S)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # one line per poll otherwise
    parser = argparse.ArgumentParser(prog="factory", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("watch", help="run labelled issues one at a time, forever")
    run = commands.add_parser("run", help="run one issue in the foreground")
    run.add_argument("--issue", type=int, required=True)
    commands.add_parser("report", help=f"write {report.PATH} from the factory's record")
    args = parser.parse_args()
    if args.command == "watch":
        watch()
    elif args.command == "report":
        report.write()
        print(f"wrote {report.PATH}")
    else:
        sys.exit(0 if run_once(args.issue) else 1)
