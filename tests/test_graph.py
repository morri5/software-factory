import sqlite3
import tempfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any, Literal
from uuid import UUID

import pytest
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from factory import config, graph, sandbox, stages, store, tracing
from factory.github import Decision
from factory.graph import IssueState, fence_frontmatter
from factory.sandbox import StageRun
from factory.stages import Stage


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
    stub("validate", {"outcome": "ok", "base_commit": "abc", "patch": b"\x00diff"})
    stub("open_pr", {"outcome": "ok"})
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


async def test_approve_resumes_after_a_restart_and_reaches_the_pr(
    tmp_path: Path, calls: list[str]
) -> None:
    await wait_for_approval(tmp_path / "checkpoints.db")
    assert calls == ["research", "plan"]
    state = await resume(tmp_path / "checkpoints.db", "approve")
    # research and plan did not re-run
    assert calls == ["research", "plan", "implement", "validate", "open_pr"]
    assert state["decision"]["action"] == "approve" and state["outcome"] == "ok"
    assert state["patch"] == b"\x00diff"  # the workspace survives the checkpoint as bytes


async def test_a_failed_validation_opens_no_pr(
    tmp_path: Path, calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    async def failed_validate(state: IssueState) -> dict[str, Any]:
        calls.append("validate")
        return {"outcome": "failed"}

    monkeypatch.setattr(graph, "validate", failed_validate)
    await wait_for_approval(tmp_path / "checkpoints.db")
    state = await resume(tmp_path / "checkpoints.db", "approve")
    assert calls == ["research", "plan", "implement", "validate"]
    assert state["outcome"] == "failed"


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


def stage_run(answer: str) -> StageRun:
    return StageRun(
        session_id=UUID(int=1), run_id="r", status="ok", reason=None, agent_message=answer,
        usage={}, cost_usd=0.0, tool_calls=0, subagent_tool_calls=0, num_turns=1, wall_s=1.0,
        started_at=None, ended_at=None, patch=b"", base_commit=None, agents_sha=None,
    )  # fmt: skip


@pytest.fixture
def stubbed_stage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[Stage, str, dict[str, str]], Awaitable[None]]:
    """`run_stage` with the session, the clone, the record and the trace stubbed; the
    thoughts repo is a temp dir."""
    monkeypatch.setattr(config, "THOUGHTS_DIR", tmp_path / "thoughts")
    monkeypatch.setattr(stages, "build_message", lambda stage, args: ("text", "sha"))
    monkeypatch.setattr(store, "record_stage_run", lambda *a: None)
    monkeypatch.setattr(tracing, "stage_run", lambda *a: None)

    async def run(stage: Stage, answer: str, files: dict[str, str]) -> None:
        async def fake_run(*a: object) -> StageRun:
            return stage_run(answer)

        def fake_materialize(*a: object) -> Path:
            tree = Path(tempfile.mkdtemp(dir=tmp_path)) / "repo"
            for rel, text in files.items():
                (tree / rel).parent.mkdir(parents=True, exist_ok=True)
                (tree / rel).write_text(text)
            return tree

        monkeypatch.setattr(sandbox, "run_stage", fake_run)
        monkeypatch.setattr(graph, "materialize", fake_materialize)
        await graph.run_stage(sqlite3.connect(":memory:"), 5, stage, UUID(int=1), "args")

    return run


def saved(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "thoughts" / "repos" / "factory-playground" / "shared" / "factory"
    return {str(p.relative_to(root)): p.read_text() for p in root.rglob("*") if p.is_file()}


async def test_an_ok_research_saves_its_document(
    tmp_path: Path, stubbed_stage: Callable[[Stage, str, dict[str, str]], Awaitable[None]]
) -> None:
    doc = "thoughts/shared/research/2026-11-20-x.md"
    answer = f'FACTORY_RESULT: {{"status": "ok", "artifact": "{doc}", "summary": "s"}}'
    await stubbed_stage("research", answer, {doc: "# Research\n"})
    assert saved(tmp_path) == {"research/2026-11-20-x.md": "# Research\n"}


async def test_a_failed_validation_saves_its_report(
    tmp_path: Path, stubbed_stage: Callable[[Stage, str, dict[str, str]], Awaitable[None]]
) -> None:
    doc = "/lab/repo/thoughts/shared/validation/v.md"
    answer = (
        f'FACTORY_RESULT: {{"status": "fail", "artifact": "{doc}", "summary": "s", '
        '"tests_exit_code": 2}'
    )
    with pytest.raises(graph.StageFailed) as failed:
        await stubbed_stage("validate", answer, {doc[len("/lab/repo/") :]: "# Failed\n"})
    assert failed.value.reason == "agent_reported_fail"
    assert saved(tmp_path) == {"validation/v.md": "# Failed\n"}


async def test_a_stage_without_a_document_saves_nothing(
    tmp_path: Path, stubbed_stage: Callable[[Stage, str, dict[str, str]], Awaitable[None]]
) -> None:
    answer = 'FACTORY_RESULT: {"status": "ok", "artifact": null, "summary": "s"}'
    with pytest.raises(graph.StageFailed) as failed:
        await stubbed_stage("plan", answer, {})
    assert failed.value.reason == "no_artifact"
    assert saved(tmp_path) == {}
