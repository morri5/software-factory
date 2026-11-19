"""`factory run --issue N` (one issue, in the foreground) and `factory watch` (the loop).

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

from factory import config, github, graph, sandbox, store

log = logging.getLogger("factory")


async def run_issue(number: int) -> dict[str, Any]:
    """Run the issue's graph until it ends; at the end its sandbox session is closed."""
    issue = github.issue(number)
    db = store.connect()
    store.start_issue(db, number, issue["title"])
    config.VAR_DIR.mkdir(parents=True, exist_ok=True)
    async with AsyncSqliteSaver.from_conn_string(str(config.VAR_DIR / "checkpoints.db")) as saver:
        app = graph.build(saver)
        thread = {"configurable": {"thread_id": f"issue-{number}"}}
        start = {"issue": number, "title": issue["title"], "body": issue["body"] or ""}
        state: dict[str, Any] = await app.ainvoke(start, thread)
        if not (await app.aget_state(thread)).next:  # reached END
            if state.get("session_id"):
                await sandbox.close_session(UUID(state["session_id"]))
            store.update_issue(db, number, finished_at=store.now())
    return state


def run_once(number: int) -> bool:
    """Run one issue; True if it ended well. An unexpected error fails the issue."""
    try:
        state = asyncio.run(run_issue(number))
    except Exception as exc:
        log.exception("issue #%d: the factory failed", number)
        try:
            github.comment(number, f"**Factory failed.** Unexpected error: `{exc!r}`"[:2000])
            github.set_status(number, "failed")
            row = (
                store.connect()
                .execute("SELECT session_id FROM issues WHERE number = ?", (number,))
                .fetchone()
            )
            if row and row["session_id"]:
                asyncio.run(sandbox.close_session(UUID(row["session_id"])))
        except Exception:
            log.exception("issue #%d: could not report the failure", number)
        return False
    log.info("issue #%d: %s", number, state.get("outcome"))
    return state.get("outcome") == "ok"


def watch() -> None:
    log.info("watching %s every %d s", config.REPO, config.POLL_S)
    while True:
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
    args = parser.parse_args()
    if args.command == "watch":
        watch()
    else:
        sys.exit(0 if run_once(args.issue) else 1)
