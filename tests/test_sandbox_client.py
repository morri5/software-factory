"""The session API client against a fake API replaying a recorded Phase 1 session log:
a `research_codebase` run on the claude_factory harness with 18 subagent calls."""

import copy
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import httpx
import pytest

from factory import sandbox
from factory.config import Limits

RECORDED: list[dict[str, Any]] = json.loads(
    (Path(__file__).parent / "fixtures" / "research_run_events.json").read_text()
)
SESSION = UUID(RECORDED[0]["session_id"])
LIMITS = Limits(max_tool_calls=300, run_timeout_s=1800, max_usd=1.0)


class FakeApi:
    """Accepts one message, then serves the log (its last message gets the posted idem key),
    a few events per poll, the way a live run grows."""

    def __init__(self, log: list[dict[str, Any]]) -> None:
        self.log = copy.deepcopy(log)
        self.messages: list[dict[str, Any]] = []
        self.polls = 0

    def handler(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if request.method == "POST" and path == "/sessions":
            return httpx.Response(201, json={"session_id": str(SESSION)})
        if request.method == "POST" and path.endswith("/messages"):
            body = json.loads(request.content)
            self.messages.append(body)
            last = [e for e in self.log if e["type"] == "user_message"][-1]
            last["idem_key"] = body["idem_key"]
            return httpx.Response(202, json={"accepted": True})
        if request.method == "GET" and path.endswith("/events"):
            self.polls += 1
            after = int(request.url.params.get("after", 0))
            visible = self.log[: self.polls * 40]
            return httpx.Response(200, json=[e for e in visible if e["seq"] > after])
        if request.method == "DELETE":
            return httpx.Response(409, json={"detail": "session is closed"})
        return httpx.Response(404)


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Any:
    def install(log: list[dict[str, Any]]) -> FakeApi:
        fake = FakeApi(log)
        transport = httpx.MockTransport(fake.handler)

        def client() -> httpx.AsyncClient:
            return httpx.AsyncClient(base_url="http://api", transport=transport)

        monkeypatch.setattr(sandbox, "_client", client)
        monkeypatch.setattr(sandbox, "POLL_S", 0)
        monkeypatch.setattr(sandbox, "WORKSPACE_GRACE_S", 0.05)  # no save after the second run
        return fake

    return install


async def test_run_stage_computes_the_metrics(api: Any) -> None:
    fake = api(RECORDED)
    run = await sandbox.run_stage(SESSION, "the message", LIMITS)
    assert fake.messages[0]["fresh_context"] is True
    assert fake.messages[0]["limits"] == LIMITS.model_dump()
    assert fake.polls > 1  # it waited for the log to grow
    assert run.status == "ok" and run.reason is None
    assert run.run_id == "36b614dbd4ef4684a580099ea590b173"
    assert run.usage == {
        "input_tokens": 43846,
        "output_tokens": 17265,
        "cache_read_input_tokens": 167936,
        "cache_creation_input_tokens": 0,
    }
    assert run.cost_usd == 0.034879
    assert (run.tool_calls, run.subagent_tool_calls) == (61, 46)
    assert run.num_turns == 16
    assert run.wall_s == 54.47
    assert run.agents_sha and run.agents_sha.startswith("d694072057a7")
    assert run.patch.startswith(b"diff --git a/thoughts/shared/research/")
    assert run.base_commit is None  # a fixture repo; https clones report their commit
    assert run.agent_message and run.agent_message.startswith("**TLDR:**")


async def test_a_second_stage_only_counts_its_own_run(api: Any) -> None:
    first = copy.deepcopy(RECORDED[:-2])  # the same session, still open after stage one
    for e in first:
        if e["type"] == "user_message":
            e["idem_key"] = "stage-one"
    seq = first[-1]["seq"]
    later = "2026-11-19T18:10:00+00:00"
    second: list[dict[str, Any]] = [
        {"type": "user_message", "payload": {"text": "plan"}, "idem_key": "x"},
        {"type": "run_started", "payload": {"run_id": "r2", "message_seq": seq + 1},
         "idem_key": None},
        {"type": "tool_call", "payload": {"id": "c", "name": "Read", "parent": None},
         "idem_key": "r2:1"},
        {"type": "run_failed", "payload": {"run_id": "r2", "reason": "timeout"},
         "idem_key": None},
    ]  # fmt: skip
    for i, e in enumerate(second, start=1):
        e.update(session_id=str(SESSION), seq=seq + i, created_at=later)
    api(first + second)
    run = await sandbox.run_stage(SESSION, "plan", LIMITS)
    assert (run.run_id, run.status, run.reason) == ("r2", "fail", "timeout")
    assert (run.tool_calls, run.cost_usd, run.agent_message) == (1, 0.0, None)
    assert run.patch.startswith(b"diff --git")  # the workspace from stage one


async def test_a_failed_provisioning_is_a_failed_run_without_patch(api: Any) -> None:
    log: list[dict[str, Any]] = [
        {"type": "session_created", "payload": {}, "idem_key": None},
        {"type": "user_message", "payload": {}, "idem_key": "k"},
        {"type": "run_started", "payload": {"run_id": "r", "message_seq": 2}, "idem_key": None},
        {"type": "run_failed", "payload": {"run_id": "r", "reason": "error"}, "idem_key": None},
        {"type": "session_failed", "payload": {"reason": "provisioning"}, "idem_key": None},
    ]
    for i, e in enumerate(log, start=1):
        e.update(session_id=str(SESSION), seq=i, created_at="2026-11-19T18:00:00+00:00")
    api(log)
    run = await sandbox.run_stage(SESSION, "x", LIMITS)
    assert (run.status, run.reason, run.patch) == ("fail", "error", b"")


async def test_create_and_close(api: Any) -> None:
    api(RECORDED)
    assert await sandbox.create_session("https://github.com/o/r.git") == SESSION
    await sandbox.close_session(SESSION)  # 409: already closed is fine
