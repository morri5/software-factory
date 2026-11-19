import json
import subprocess
from typing import Any

import pytest

from factory import config, github


class FakeGh:
    def __init__(self, outputs: list[str]) -> None:
        self.calls: list[tuple[list[str], str | None]] = []
        self.outputs = outputs

    def __call__(self, argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["check"] is True and kwargs.get("shell") is not True
        self.calls.append((argv, kwargs.get("input")))
        return subprocess.CompletedProcess(argv, 0, self.outputs.pop(0), "")


@pytest.fixture
def gh(monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "REPO", "o/r")

    def install(*outputs: str) -> FakeGh:
        fake = FakeGh(list(outputs))
        monkeypatch.setattr(subprocess, "run", fake)
        return fake

    return install


def test_comment_posts_the_body_through_stdin(gh: Any) -> None:
    fake = gh(json.dumps({"id": 5, "created_at": "2026-11-19T10:00:00Z"}))
    body = "text with `$(rm -rf /)` and 'quotes'"
    assert github.comment(3, body)["id"] == 5
    argv, stdin = fake.calls[0]
    assert argv == [
        "gh", "api", "--method", "POST", "repos/o/r/issues/3/comments", "--input", "-",
    ]  # fmt: skip
    assert json.loads(stdin or "") == {"body": body}


def test_issue(gh: Any) -> None:
    fake = gh('{"number": 1, "title": "t", "body": "b", "labels": []}')
    assert github.issue(1)["title"] == "t"
    assert fake.calls[0][0] == [
        "gh", "issue", "view", "1", "-R", "o/r", "--json", "number,title,body,labels",
    ]  # fmt: skip


def test_queued_issues_skips_status_labels_and_sorts_oldest_first(gh: Any) -> None:
    issues = [
        {"number": 3, "createdAt": "2026-11-19T03:00:00Z", "labels": [{"name": "factory"}]},
        {"number": 2, "createdAt": "2026-11-19T02:00:00Z",
         "labels": [{"name": "factory"}, {"name": "factory:research"}]},
        {"number": 1, "createdAt": "2026-11-19T01:00:00Z",
         "labels": [{"name": "factory"}, {"name": "bug"}]},
    ]  # fmt: skip
    fake = gh(json.dumps(issues))
    assert [i["number"] for i in github.queued_issues()] == [1, 3]
    argv = fake.calls[0][0]
    assert argv[:7] == ["gh", "issue", "list", "-R", "o/r", "--label", "factory"]
    assert argv[7:9] == ["--state", "open"]


def test_set_status_replaces_the_old_status_label(gh: Any) -> None:
    labels = [{"name": "bug"}, {"name": "factory"}, {"name": "factory:research"}]
    fake = gh(json.dumps({"number": 4, "labels": labels}), "")
    github.set_status(4, "failed")
    assert fake.calls[1][0] == [
        "gh", "issue", "edit", "4", "-R", "o/r",
        "--add-label", "factory:failed", "--remove-label", "factory:research",
    ]  # fmt: skip


def test_set_status_without_an_old_label(gh: Any) -> None:
    fake = gh(json.dumps({"number": 4, "labels": [{"name": "factory"}]}), "")
    github.set_status(4, "research")
    assert fake.calls[1][0][-2:] == ["--add-label", "factory:research"]
