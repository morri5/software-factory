"""HTTP client for the session API of `sandboxed-agents-lab` (`make api` there).

One session per issue, on the `claude_factory` harness. Each stage is one message
with `fresh_context`, so every stage agent starts with a clean context in the same
VM and working tree. A stage's metrics come from the session's event log.
"""

import asyncio
import base64
import datetime
import time
from typing import Any, Literal
from uuid import UUID, uuid4

import httpx
from pydantic import BaseModel

from factory import config
from factory.config import Limits

HARNESS = "claude_factory"
POLL_S = 5.0
# The workspace is saved right after the run ends (about 0.2 s measured); without a
# change there is no `workspace_saved`, so the wait for one is bounded.
WORKSPACE_GRACE_S = 30.0
# On top of the run's own timeout: VM provisioning and the workspace save.
SLACK_S = 300
FINAL = ("run_finished", "run_failed")
SESSION_ENDS = ("session_closed", "session_failed")


class StageRun(BaseModel):
    session_id: UUID
    run_id: str
    status: Literal["ok", "fail", "error"]  # error: the session could not run it at all
    reason: str | None
    agent_message: str | None
    usage: dict[str, int]
    cost_usd: float
    tool_calls: int
    subagent_tool_calls: int
    num_turns: int | None
    wall_s: float
    started_at: str | None  # the run's first and last event (ISO), for the trace
    ended_at: str | None
    patch: bytes
    base_commit: str | None  # the commit the session cloned; the patch applies on it
    agents_sha: str | None


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(base_url=config.API_URL, timeout=30)


async def create_session(repo_url: str) -> UUID:
    async with _client() as client:
        body = {"repo_url": repo_url, "harness": HARNESS}
        r = (await client.post("/sessions", json=body)).raise_for_status()
        return UUID(r.json()["session_id"])


async def close_session(session_id: UUID) -> None:
    """Queued behind any pending run; a session that is already closed is fine."""
    async with _client() as client:
        r = await client.delete(f"/sessions/{session_id}")
        if r.status_code != 409:
            r.raise_for_status()


async def events(session_id: UUID, after: int = 0) -> list[dict[str, Any]]:
    async with _client() as client:
        r = await client.get(f"/sessions/{session_id}/events", params={"after": after})
        result: list[dict[str, Any]] = r.raise_for_status().json()
        return result


async def run_stage(session_id: UUID, text: str, limits: Limits) -> StageRun:
    """Send one fresh-context message and wait for its run and workspace snapshot."""
    idem_key = str(uuid4())
    message = {
        "text": text,
        "idem_key": idem_key,
        "fresh_context": True,
        "limits": limits.model_dump(),
    }
    async with _client() as client:
        r = await client.post(f"/sessions/{session_id}/messages", json=message)
        r.raise_for_status()
    log: list[dict[str, Any]] = []
    deadline = time.monotonic() + limits.run_timeout_s + SLACK_S
    ended_at: float | None = None
    while time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        log += await events(session_id, log[-1]["seq"] if log else 0)
        if not _done(log, idem_key):
            continue
        ended_at = ended_at or time.monotonic()
        if _saved(log, idem_key) or time.monotonic() - ended_at > WORKSPACE_GRACE_S:
            return stage_run(log, idem_key)
    return stage_run(log, idem_key, timed_out=True)


def _runs(log: list[dict[str, Any]], idem_key: str) -> tuple[int | None, list[dict[str, Any]]]:
    """The seq of the message, and the runs started for it (a re-run after a crash
    of the API is a second one)."""
    message = next(
        (e for e in log if e["type"] == "user_message" and e["idem_key"] == idem_key), None
    )
    if message is None:
        return None, []
    seq = message["seq"]
    starts = [
        e for e in log if e["type"] == "run_started" and e["payload"].get("message_seq") == seq
    ]
    return seq, starts


def _of_run(e: dict[str, Any], run_id: str) -> bool:
    """Runner events carry the run in their idem key, session events in the payload."""
    return (e["idem_key"] or "").startswith(f"{run_id}:") or e["payload"].get("run_id") == run_id


def _terminal(log: list[dict[str, Any]], run_id: str) -> dict[str, Any] | None:
    return next((e for e in log if e["type"] in FINAL and _of_run(e, run_id)), None)


def _done(log: list[dict[str, Any]], idem_key: str) -> bool:
    _, starts = _runs(log, idem_key)
    if any(e["type"] in SESSION_ENDS for e in log):
        return True
    return bool(starts) and _terminal(log, starts[-1]["payload"]["run_id"]) is not None


def _saved(log: list[dict[str, Any]], idem_key: str) -> bool:
    _, starts = _runs(log, idem_key)
    if not starts:
        return any(e["type"] in SESSION_ENDS for e in log)
    end = _terminal(log, starts[-1]["payload"]["run_id"])
    after = end["seq"] if end else starts[-1]["seq"]
    return any(e["seq"] > after and e["type"] in ("workspace_saved", *SESSION_ENDS) for e in log)


def _seconds(start: dict[str, Any], end: dict[str, Any]) -> float:
    t0 = datetime.datetime.fromisoformat(start["created_at"])
    t1 = datetime.datetime.fromisoformat(end["created_at"])
    return round((t1 - t0).total_seconds(), 2)


def stage_run(log: list[dict[str, Any]], idem_key: str, timed_out: bool = False) -> StageRun:
    """The stage's result and metrics from the session log (all events up to now)."""
    session_id = UUID(log[0]["session_id"]) if log else UUID(int=0)
    _, starts = _runs(log, idem_key)
    run_id = starts[-1]["payload"]["run_id"] if starts else ""
    end = _terminal(log, run_id) if starts else None
    calls = [e for e in log if e["type"] == "tool_call" and _of_run(e, run_id)]
    answer = next(
        (e for e in reversed(log) if e["type"] == "agent_message" and _of_run(e, run_id)), None
    )
    saved = [e for e in log if e["type"] == "workspace_saved"]
    attached = [e for e in log if e["type"] == "sandbox_attached"]
    payload = end["payload"] if end else {}
    if end is not None and end["type"] == "run_finished":
        status: Literal["ok", "fail", "error"] = "ok"
        reason = None
    elif end is not None:
        status, reason = "fail", payload.get("reason")
    else:
        failed = next((e for e in log if e["type"] == "session_failed"), None)
        status = "error"
        reason = "timeout" if timed_out else (failed or {}).get("payload", {}).get("reason")
        reason = reason or "no_run"
    usage = payload.get("usage") or {}
    return StageRun(
        session_id=session_id,
        run_id=run_id,
        status=status,
        reason=reason,
        agent_message=answer["payload"].get("text") if answer else None,
        usage={k: int(v or 0) for k, v in usage.items()},
        cost_usd=float(payload.get("cost_usd") or 0.0),
        tool_calls=len(calls),
        subagent_tool_calls=sum(c["payload"].get("parent") is not None for c in calls),
        num_turns=payload.get("num_turns"),
        wall_s=_seconds(starts[-1], end) if starts and end else 0.0,
        started_at=starts[-1]["created_at"] if starts else None,
        ended_at=end["created_at"] if end else None,
        patch=base64.b64decode(saved[-1]["payload"]["patch_b64"]) if saved else b"",
        base_commit=attached[-1]["payload"].get("commit") if attached else None,
        agents_sha=payload.get("agents_sha"),
    )
