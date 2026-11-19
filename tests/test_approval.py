import json
import subprocess
from typing import Any

import pytest

from factory import config, github

PLAN_AT = "2026-11-19T10:00:00Z"


def comment(cid: int, login: str, created_at: str, body: str) -> dict[str, Any]:
    return {"id": cid, "user": {"login": login}, "created_at": created_at, "body": body}


@pytest.fixture
def comments(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Serve the given comments as `gh api --paginate --slurp` does: a list of pages."""
    monkeypatch.setattr(config, "REPO", "o/r")
    calls: list[list[str]] = []

    def install(*pages: list[dict[str, Any]]) -> list[list[str]]:
        def fake(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, json.dumps(list(pages)), "")

        monkeypatch.setattr(subprocess, "run", fake)
        return calls

    return install


def test_reads_every_page_of_the_issue_comments(comments: Any) -> None:
    calls = comments(
        [comment(1, "morri5", "2026-11-19T09:00:00Z", "hi")],
        [comment(2, "morri5", "2026-11-19T11:00:00Z", "/approve")],
    )
    decision = github.find_decision(3, PLAN_AT, "morri5")
    assert decision == {
        "action": "approve",
        "reason": "",
        "comment_id": 2,
        "created_at": "2026-11-19T11:00:00Z",
    }
    assert calls[0] == ["gh", "api", "repos/o/r/issues/3/comments", "--paginate", "--slurp"]


def test_ignores_other_authors(comments: Any) -> None:
    comments([comment(1, "someone", "2026-11-19T11:00:00Z", "/approve")])
    assert github.find_decision(3, PLAN_AT, "morri5") is None


def test_ignores_comments_before_the_plan(comments: Any) -> None:
    comments(
        [
            comment(1, "morri5", "2026-11-19T09:59:59Z", "/approve"),
            comment(2, "morri5", PLAN_AT, "/approve"),  # not after the plan comment
        ]
    )
    assert github.find_decision(3, PLAN_AT, "morri5") is None


@pytest.mark.parametrize(
    "body", ["I will /approve this later", "please /reject", "/approved", "> /approve"]
)
def test_the_command_must_start_the_comment(comments: Any, body: str) -> None:
    comments([comment(1, "morri5", "2026-11-19T11:00:00Z", body)])
    assert github.find_decision(3, PLAN_AT, "morri5") is None


def test_picks_the_first_valid_decision(comments: Any) -> None:
    comments(
        [
            comment(4, "morri5", "2026-11-19T13:00:00Z", "/approve"),
            comment(3, "morri5", "2026-11-19T12:00:00Z", "/reject Too broad."),
            comment(2, "morri5", "2026-11-19T11:00:00Z", "Looks fine, reading on."),
        ]
    )
    decision = github.find_decision(3, PLAN_AT, "morri5")
    assert decision is not None
    assert (decision["action"], decision["comment_id"]) == ("reject", 3)


def test_parses_the_reject_reason(comments: Any) -> None:
    body = "  /reject  The plan changes the CSV format.\nKeep it as it is.  "
    comments([comment(1, "morri5", "2026-11-19T11:00:00Z", body)])
    decision = github.find_decision(3, PLAN_AT, "morri5")
    assert decision is not None and decision["action"] == "reject"
    assert decision["reason"] == "The plan changes the CSV format.\nKeep it as it is."


def test_approve_with_punctuation(comments: Any) -> None:
    comments([comment(1, "morri5", "2026-11-19T11:00:00Z", "/approve!")])
    decision = github.find_decision(3, PLAN_AT, "morri5")
    assert decision is not None and decision["action"] == "approve"
