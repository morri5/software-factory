"""One-off: save the stage documents of issues that ran before the factory kept them.

For each issue in `var/factory.db` with a session, rebuild the session's last saved
workspace, save its research, plan and validation documents into the thoughts repo and
back them up. Needs the session API running (`make -C ../sandboxed-agents api`).

Run with `uv run python scripts/backfill_docs.py`.
"""

import asyncio
import base64
import logging
import shutil
from uuid import UUID

from factory import config, memory, sandbox, store
from factory.workspace import materialize

FOLDERS = ("research", "plans", "validation")


async def backfill(number: int, session_id: UUID) -> None:
    log = await sandbox.events(session_id)
    saved = [e for e in log if e["type"] == "workspace_saved"]
    attached = [e for e in log if e["type"] == "sandbox_attached"]
    if not saved:
        print(f"#{number}: no saved workspace")
        return
    patch = base64.b64decode(saved[-1]["payload"]["patch_b64"])
    commit = attached[-1]["payload"].get("commit") if attached else None
    tree = materialize(config.repo_url(), commit, patch)
    try:
        for folder in FOLDERS:
            for doc in sorted((tree / "thoughts" / "shared" / folder).glob("*.md")):
                print(f"#{number}: {memory.save(folder, doc.name, doc.read_text())}")
    finally:
        shutil.rmtree(tree.parent)
    memory.backup(number)


async def main() -> None:
    rows = store.connect().execute(
        "SELECT number, session_id FROM issues WHERE session_id IS NOT NULL ORDER BY number"
    )
    for row in rows.fetchall():
        await backfill(row["number"], UUID(row["session_id"]))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
