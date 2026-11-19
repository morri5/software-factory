"""The pull request: the session's workspace, without `thoughts/`, committed and pushed
by the host. The VM never sees a GitHub credential.

A diff that touches `.github/` is refused (workflow changes need a human), and so is
an empty one (nothing was implemented).
"""

import shutil
import sqlite3
from pathlib import Path

from factory import config, github
from factory.workspace import _git, materialize

TITLE_CHARS = 60


class PrRefused(Exception):
    """The workspace cannot become a PR (empty, or touches `.github/`)."""


def branch(number: int) -> str:
    return f"factory/issue-{number}"


def commit_message(number: int, title: str, labels: list[str]) -> str:
    kind = "feat" if "enhancement" in labels else "fix"
    return f"{kind}: {title.strip()[:TITLE_CHARS].rstrip()} (#{number})"


def metrics_table(runs: list[sqlite3.Row]) -> str:
    lines = [
        "| Stage | Status | Cost | Tool calls (by subagents) | Turns | Wall time |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    for r in runs:
        lines.append(
            f"| {r['stage']} | {r['status']} | ${r['cost_usd']:.4f} | "
            f"{r['tool_calls']} ({r['subagent_tool_calls']}) | {r['num_turns']} | "
            f"{r['wall_s']:.0f} s |"
        )
    cost = sum(r["cost_usd"] for r in runs)
    calls = sum(r["tool_calls"] for r in runs)
    sub = sum(r["subagent_tool_calls"] for r in runs)
    wall = sum(r["wall_s"] for r in runs)
    lines.append(f"| **total** | | **${cost:.4f}** | **{calls} ({sub})** | | **{wall:.0f} s** |")
    return "\n".join(lines)


def pr_body(number: int, plan_summary: str, validate_summary: str, table: str) -> str:
    return (
        f"Closes #{number}\n\n"
        f"## Plan\n\n{plan_summary}\n\n"
        f"## Validation\n\n{validate_summary}\n\n"
        f"## Stage runs\n\n{table}\n\n"
        "<sub>Opened by the factory. The plan was approved on the issue; review this "
        "change like any other.</sub>"
    )


def open_pr(
    number: int,
    message: str,
    body: str,
    base_commit: str | None,
    patch: bytes,
    repo: str | None = None,
) -> str:
    """Commit the workspace's code changes on `factory/issue-<n>`, push, open the PR."""
    tree = materialize(repo or config.repo_url(), base_commit, patch)
    try:
        head = _commit_and_push(tree, branch(number), message)
    finally:
        shutil.rmtree(tree.parent)
    return github.create_pr(head, message, body)


def _commit_and_push(tree: Path, head: str, message: str) -> str:
    _git("checkout", "-q", "-b", head, cwd=tree)
    # Only the repo's own ignores apply, as in the VM; a global one (often `thoughts`)
    # would make git refuse the pathspec.
    _git("-c", "core.excludesFile=/dev/null", "add", "-A", "--", ".", ":(exclude)thoughts",
         cwd=tree)  # fmt: skip
    changed = _git("diff", "--cached", "--name-only", cwd=tree).splitlines()
    if not changed:
        raise PrRefused("the workspace has no code changes")
    workflow = [p for p in changed if p.startswith(".github/")]
    if workflow:
        raise PrRefused(f"workflow changes need a human: {', '.join(workflow)}")
    _git("commit", "-q", "--no-verify", "-m", message, cwd=tree)
    _git("push", "-q", "-u", "origin", head, cwd=tree)
    return head
