"""Thin wrappers over the `gh` CLI (issues, labels, comments, the approval).

Always an argv list, never a shell string: issue text is untrusted and must never
reach a shell. Bodies go through stdin.
"""

import datetime
import json
import re
import subprocess
from typing import Any, Literal, TypedDict

from factory import config

TRIGGER = "factory"
STATUS_PREFIX = "factory:"
STATUSES = (
    "research", "awaiting-approval", "implement", "pr-open", "merged", "failed", "rejected",
)  # fmt: skip


def _gh(*args: str, stdin: str | None = None) -> str:
    try:
        result = subprocess.run(
            ["gh", *args], input=stdin, check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as exc:  # gh's stderr says why ("label not found")
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {exc.stderr.strip()}") from exc
    return result.stdout


def ensure_labels() -> list[str]:
    """Create the trigger and status labels the repo lacks; returns the ones created."""
    out = _gh("label", "list", "-R", config.REPO, "--json", "name", "--limit", "1000")
    existing = {lb["name"] for lb in json.loads(out)}
    wanted = [TRIGGER] + [STATUS_PREFIX + s for s in STATUSES]
    missing = [name for name in wanted if name not in existing]
    for name in missing:
        _gh("label", "create", name, "-R", config.REPO, "--color", "8250df")
    return missing


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


def create_pr(head: str, title: str, body: str) -> str:
    """Open a PR from `head` against the default branch; returns its URL."""
    out = _gh(
        "pr", "create", "-R", config.REPO, "--head", head, "--title", title,
        "--body-file", "-",
        stdin=body,
    )  # fmt: skip
    return out.strip().splitlines()[-1]


def merged_at(pr_url: str) -> str | None:
    """When the PR was merged (ISO time), or None while it is open or closed unmerged."""
    out = _gh("pr", "view", pr_url, "--json", "mergedAt")
    merged: str | None = json.loads(out)["mergedAt"]
    return merged or None


class Decision(TypedDict):
    """The approver's answer to a plan: a `/approve` or `/reject <reason>` comment."""

    action: Literal["approve", "reject"]
    reason: str
    comment_id: int
    created_at: str


COMMAND = re.compile(r"/(approve|reject)\b(.*)", re.DOTALL)


def _when(timestamp: str) -> datetime.datetime:
    return datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))


def find_decision(number: int, after: str, approver: str) -> Decision | None:
    """The first `/approve` or `/reject` by `approver` created after `after` (ISO time).

    Comments by anyone else, or created before `after`, are ignored; only the creation
    time counts, so editing an older comment cannot approve. The command must start the
    comment: a sentence that mentions `/approve` later on is not a decision.
    """
    out = _gh(
        "api", f"repos/{config.REPO}/issues/{number}/comments", "--paginate", "--slurp",
    )  # fmt: skip
    pages: list[list[dict[str, Any]]] = json.loads(out)
    since = _when(after)
    candidates = sorted(
        (
            c
            for page in pages
            for c in page
            if c["user"]["login"] == approver and _when(c["created_at"]) > since
        ),
        key=lambda c: (_when(c["created_at"]), c["id"]),
    )
    for c in candidates:
        match = COMMAND.match((c["body"] or "").strip())
        if match:
            action: Literal["approve", "reject"] = (
                "approve" if match.group(1) == "approve" else "reject"
            )
            return Decision(
                action=action,
                reason=match.group(2).strip(),
                comment_id=c["id"],
                created_at=c["created_at"],
            )
    return None
