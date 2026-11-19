"""One LangGraph thread per issue (`thread_id = issue-<n>`), checkpointed in SQLite.

For now the graph is `research -> plan -> approve -> implement (a stub) | rejected`.
A stage node runs its command in the issue's sandbox session, records the run,
reads the stage's document out of the saved workspace and reports on the issue.
Any failure labels the issue `factory:failed` with a comment that names the reason
and the session, and ends the graph.

`approve` waits at `interrupt()` for the approver's decision; `factory watch` resumes
the thread with `Command(resume=decision)` once the decision is on the issue. The
checkpoint keeps the thread across restarts and days of waiting.
"""

import shutil
import sqlite3
from dataclasses import dataclass
from typing import Any, Literal, TypedDict
from uuid import UUID

from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from factory import config, github, sandbox, stages, store
from factory.sandbox import StageRun
from factory.stages import FactoryResult, NoResult, Stage
from factory.workspace import ArtifactError, materialize, read_artifact

MAX_DOC_CHARS = 60_000  # a GitHub comment holds 65,536


class IssueState(TypedDict, total=False):
    issue: int
    title: str
    body: str
    session_id: str
    outcome: Literal["ok", "failed", "rejected"]
    research_path: str
    plan_path: str
    plan_comment_at: str
    decision: github.Decision


@dataclass
class StageFailed(Exception):
    reason: str
    run: StageRun | None = None
    summary: str | None = None


@dataclass
class StageDone:
    run: StageRun
    result: FactoryResult
    path: str
    document: str


def metrics(run: StageRun) -> str:
    return (
        f"${run.cost_usd:.4f}, {run.tool_calls} tool calls "
        f"({run.subagent_tool_calls} by subagents), {run.num_turns} turns, {run.wall_s:.0f} s"
    )


def fence_frontmatter(document: str) -> str:
    """Show YAML frontmatter as a code block; bare, GitHub renders it as a heading."""
    if not document.startswith("---\n"):
        return document
    meta, sep, body = document[4:].partition("\n---\n")
    return f"```yaml\n{meta}\n```\n{body}" if sep else document


def stage_comment(stage: Stage, done: StageDone) -> str:
    document = fence_frontmatter(done.document)
    if len(document) > MAX_DOC_CHARS:
        document = (
            document[:MAX_DOC_CHARS]
            + f"\n\n*(Truncated at {MAX_DOC_CHARS:,} characters; the full document is "
            "in the session's workspace.)*"
        )
    return (
        f"**Factory: {stage}**\n\n{done.result.summary}\n\n"
        f"<details><summary>{stage.capitalize()} document: <code>{done.path}</code></summary>"
        f"\n\n{document}\n\n</details>\n\n"
        f"<sub>{metrics(done.run)}. Sandbox session <code>{done.run.session_id}</code>.</sub>"
    )


def failure_comment(stage: Stage, session_id: UUID, failed: StageFailed) -> str:
    lines = [f"**Factory: {stage} failed.** Reason: `{failed.reason}`."]
    if failed.summary:
        lines.append(failed.summary)
    if failed.run is not None:
        lines.append(f"<sub>{metrics(failed.run)}.</sub>")
    lines.append(f"Sandbox session `{session_id}`.")
    return "\n\n".join(lines)


async def run_stage(
    db: sqlite3.Connection, issue: int, stage: Stage, session_id: UUID, args: str
) -> StageDone:
    """Run the stage and read its document; every run is recorded, also a failed one."""
    text, commands_sha = stages.build_message(stage, args)
    started_at = store.now()
    run = await sandbox.run_stage(session_id, text, config.LIMITS[stage])

    def fail(reason: str, summary: str | None = None) -> StageFailed:
        store.record_stage_run(db, issue, stage, run, commands_sha, started_at, "fail", reason)
        return StageFailed(reason, run, summary)

    if run.status != "ok":
        store.record_stage_run(db, issue, stage, run, commands_sha, started_at)
        raise StageFailed(run.reason or run.status, run)
    try:
        result = stages.parse_result(run.agent_message)
    except NoResult as exc:
        raise fail("no_result", str(exc)) from exc
    if result.status != "ok":
        raise fail("agent_reported_fail", result.summary)
    if not result.artifact:
        raise fail("no_artifact", result.summary)
    tree = materialize(config.repo_url(), run.base_commit, run.patch)
    try:
        document = read_artifact(tree, result.artifact)
    except ArtifactError as exc:
        raise fail("artifact_missing", str(exc)) from exc
    finally:
        shutil.rmtree(tree.parent)
    store.record_stage_run(db, issue, stage, run, commands_sha, started_at)
    return StageDone(run, result, result.artifact, document)


def stage_failed(
    db: sqlite3.Connection, n: int, stage: Stage, session_id: UUID, failed: StageFailed
) -> dict[str, Any]:
    github.comment(n, failure_comment(stage, session_id, failed))
    github.set_status(n, "failed")
    store.update_issue(db, n, state="failed")
    return {"outcome": "failed"}


async def research(state: IssueState) -> dict[str, Any]:
    n = state["issue"]
    db = store.connect()
    github.set_status(n, "research")
    session_id = await sandbox.create_session(config.repo_url())
    store.update_issue(db, n, session_id=str(session_id), state="research")
    args = stages.research_args(n, state["title"], state["body"])
    try:
        done = await run_stage(db, n, "research", session_id, args)
    except StageFailed as failed:
        return {"session_id": str(session_id)} | stage_failed(db, n, "research", session_id, failed)
    github.comment(n, stage_comment("research", done))
    return {"session_id": str(session_id), "outcome": "ok", "research_path": done.path}


APPROVAL_HELP = (
    "Comment `/approve` to implement this plan, or `/reject <reason>` to stop here. "
    "Only a comment by {approver} posted after this one counts."
)


async def plan(state: IssueState) -> dict[str, Any]:
    n = state["issue"]
    db = store.connect()
    session_id = UUID(state["session_id"])
    store.update_issue(db, n, state="plan")
    args = stages.plan_args(n, state["title"], state["body"], state["research_path"])
    try:
        done = await run_stage(db, n, "plan", session_id, args)
    except StageFailed as failed:
        return stage_failed(db, n, "plan", session_id, failed)
    help_line = APPROVAL_HELP.format(approver=config.APPROVER)
    posted = github.comment(n, f"{stage_comment('plan', done)}\n\n{help_line}")
    store.update_issue(
        db,
        n,
        state="awaiting_approval",
        plan_comment_id=posted["id"],
        plan_comment_at=posted["created_at"],
    )
    github.set_status(n, "awaiting-approval")
    return {"outcome": "ok", "plan_path": done.path, "plan_comment_at": posted["created_at"]}


def approve(state: IssueState) -> dict[str, Any]:
    """Wait for the decision. The node re-runs on resume, so nothing here has side effects."""
    decision: github.Decision = interrupt(
        {"issue": state["issue"], "plan_comment_at": state["plan_comment_at"]}
    )
    return {"decision": decision}


async def implement(state: IssueState) -> dict[str, Any]:
    """A stub until the implement stage exists: records the approval and ends."""
    n = state["issue"]
    store.update_issue(
        store.connect(), n, state="approved", approved_at=state["decision"]["created_at"]
    )
    github.set_status(n, "approved")
    return {"outcome": "ok"}


async def rejected(state: IssueState) -> dict[str, Any]:
    n = state["issue"]
    reason = state["decision"]["reason"] or "no reason given"
    github.comment(n, f"**Factory: plan rejected.** Reason: {reason}\n\nThe run ends here.")
    github.set_status(n, "rejected")
    store.update_issue(store.connect(), n, state="rejected")
    return {"outcome": "rejected"}


def _ok(state: IssueState) -> bool:
    return state.get("outcome") == "ok"


def build(checkpointer: BaseCheckpointSaver[Any]) -> Any:
    graph = StateGraph(IssueState)
    graph.add_node("research", research)
    graph.add_node("plan", plan)
    graph.add_node("approve", approve)
    graph.add_node("implement", implement)
    graph.add_node("rejected", rejected)
    graph.add_edge(START, "research")
    graph.add_conditional_edges("research", lambda s: "plan" if _ok(s) else END, ["plan", END])
    graph.add_conditional_edges("plan", lambda s: "approve" if _ok(s) else END, ["approve", END])
    graph.add_conditional_edges(
        "approve",
        lambda s: "implement" if s["decision"]["action"] == "approve" else "rejected",
        ["implement", "rejected"],
    )
    graph.add_edge("implement", END)
    graph.add_edge("rejected", END)
    return graph.compile(checkpointer=checkpointer)
