"""Thin wrappers over the `gh` CLI (issues, labels, comments).

Always an argv list, never a shell string: issue text is untrusted and must never
reach a shell. Bodies go through stdin.
"""

import json
import subprocess
from typing import Any

from factory import config

TRIGGER = "factory"
STATUS_PREFIX = "factory:"


def _gh(*args: str, stdin: str | None = None) -> str:
    result = subprocess.run(["gh", *args], input=stdin, check=True, capture_output=True, text=True)
    return result.stdout


def issue(number: int) -> dict[str, Any]:
    out = _gh("issue", "view", str(number), "-R", config.REPO, "--json", "number,title,body,labels")
    data: dict[str, Any] = json.loads(out)
    return data


def queued_issues() -> list[dict[str, Any]]:
    """Open issues labelled `factory` without a `factory:*` status label, oldest first."""
    out = _gh(
        "issue", "list", "-R", config.REPO, "--label", TRIGGER, "--state", "open",
        "--json", "number,title,labels,createdAt", "--limit", "100",
    )  # fmt: skip
    issues: list[dict[str, Any]] = json.loads(out)
    waiting = [
        i for i in issues if not any(lb["name"].startswith(STATUS_PREFIX) for lb in i["labels"])
    ]
    return sorted(waiting, key=lambda i: (i["createdAt"], i["number"]))


def comment(number: int, body: str) -> dict[str, Any]:
    """Post a comment; returns GitHub's JSON for it (id, created_at, html_url, ...)."""
    out = _gh(
        "api", "--method", "POST", f"repos/{config.REPO}/issues/{number}/comments",
        "--input", "-",
        stdin=json.dumps({"body": body}),
    )  # fmt: skip
    data: dict[str, Any] = json.loads(out)
    return data


def set_status(number: int, status: str) -> None:
    """Make `factory:<status>` the issue's only status label."""
    label = STATUS_PREFIX + status
    current = [lb["name"] for lb in issue(number)["labels"]]
    stale = [n for n in current if n.startswith(STATUS_PREFIX) and n != label]
    args = ["issue", "edit", str(number), "-R", config.REPO, "--add-label", label]
    if stale:
        args += ["--remove-label", ",".join(stale)]
    _gh(*args)
