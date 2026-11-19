from pathlib import Path
from typing import Any, Literal

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from factory import graph
from factory.github import Decision
from factory.graph import IssueState, fence_frontmatter


def test_frontmatter_becomes_a_yaml_block() -> None:
    doc = "---\ndate: 2026-11-19\nresearcher: factory\n---\n\n# Research\n"
    assert (
        fence_frontmatter(doc)
        == "```yaml\ndate: 2026-11-19\nresearcher: factory\n```\n\n# Research\n"
    )


def test_document_without_frontmatter_is_unchanged() -> None:
    assert fence_frontmatter("# Research\n---\n") == "# Research\n---\n"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Stub every node with side effects; `approve` stays real."""
    called: list[str] = []

    def stub(name: str, update: dict[str, Any]) -> None:
        async def node(state: IssueState) -> dict[str, Any]:
            called.append(name)
            return update

        monkeypatch.setattr(graph, name, node)

    stub("research", {"session_id": "s", "outcome": "ok", "research_path": "r.md"})
    stub("plan", {"outcome": "ok", "plan_path": "p.md", "plan_comment_at": "2026-11-19T10:00:00Z"})
    stub("implement", {"outcome": "ok"})
    stub("rejected", {"outcome": "rejected"})
    return called


THREAD = {"configurable": {"thread_id": "issue-2"}}


def decision(action: Literal["approve", "reject"]) -> Decision:
    return {"action": action, "reason": "r", "comment_id": 1, "created_at": "2026-11-19T11:00:00Z"}


async def wait_for_approval(db: Path) -> None:
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        app = graph.build(saver)
        state = await app.ainvoke({"issue": 2, "title": "t", "body": "b"}, THREAD)
        assert state["__interrupt__"][0].value == {
            "issue": 2,
            "plan_comment_at": "2026-11-19T10:00:00Z",
        }
        assert (await app.aget_state(THREAD)).next == ("approve",)


async def resume(db: Path, action: Literal["approve", "reject"]) -> dict[str, Any]:
    """A new saver on the same file: the factory restarted while the issue waited."""
    async with AsyncSqliteSaver.from_conn_string(str(db)) as saver:
        app = graph.build(saver)
        state: dict[str, Any] = await app.ainvoke(Command(resume=decision(action)), THREAD)
        assert not (await app.aget_state(THREAD)).next
        return state


async def test_approve_resumes_after_a_restart_and_reaches_implement(
    tmp_path: Path, calls: list[str]
) -> None:
    await wait_for_approval(tmp_path / "checkpoints.db")
    assert calls == ["research", "plan"]
    state = await resume(tmp_path / "checkpoints.db", "approve")
    assert calls == ["research", "plan", "implement"]  # research and plan did not re-run
    assert state["decision"]["action"] == "approve" and state["outcome"] == "ok"


async def test_reject_never_reaches_implement(tmp_path: Path, calls: list[str]) -> None:
    await wait_for_approval(tmp_path / "checkpoints.db")
    state = await resume(tmp_path / "checkpoints.db", "reject")
    assert calls == ["research", "plan", "rejected"]
    assert state["outcome"] == "rejected"


async def test_a_failed_stage_ends_before_the_approval(
    tmp_path: Path, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_research(state: IssueState) -> dict[str, Any]:
        calls.append("research")
        return {"session_id": "s", "outcome": "failed"}

    monkeypatch.setattr(graph, "research", failed_research)
    async with AsyncSqliteSaver.from_conn_string(str(tmp_path / "c.db")) as saver:
        app = graph.build(saver)
        await app.ainvoke({"issue": 2, "title": "t", "body": "b"}, THREAD)
        assert not (await app.aget_state(THREAD)).next
    assert calls == ["research"]
