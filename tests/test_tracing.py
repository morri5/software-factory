import json
import logging
import uuid
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from factory import config, store, tracing
from factory.sandbox import StageRun

STARTED = "2026-11-19T20:42:14+00:00"


@pytest.fixture
def db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    monkeypatch.setattr(config, "VAR_DIR", tmp_path)
    conn = store.connect()
    conn.execute(
        "INSERT INTO issues (number, title, state, thread_id, started_at) "
        "VALUES (3, 'CSV export misses the header row', 'implement', 'issue-3', ?)",
        (STARTED,),
    )
    conn.commit()
    return conn


@pytest.fixture
def spans(monkeypatch: pytest.MonkeyPatch) -> InMemorySpanExporter:
    """Langfuse with an in-memory exporter. The SDK keeps one client per public key, so
    each test gets its own key."""
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", f"pk-test-{uuid.uuid4()}")
    monkeypatch.setattr(tracing, "_client", None)
    monkeypatch.setattr(tracing, "_reachable", lambda client: True)
    exporter = InMemorySpanExporter()
    tracing.connect(exporter)
    return exporter


def run(**changes: Any) -> StageRun:
    fields: dict[str, Any] = {
        "session_id": UUID(int=7),
        "run_id": "r1",
        "status": "ok",
        "reason": None,
        "agent_message": "done",
        "usage": {
            "input_tokens": 43846,
            "output_tokens": 17265,
            "cache_read_input_tokens": 167936,
            "cache_creation_input_tokens": 0,
        },
        "cost_usd": 0.034879,
        "tool_calls": 61,
        "subagent_tool_calls": 46,
        "num_turns": 16,
        "wall_s": 54.47,
        "started_at": "2026-11-19T17:58:44.457953+00:00",
        "ended_at": "2026-11-19T17:59:38.930955+00:00",
        "patch": b"",
        "base_commit": None,
        "agents_sha": "d694",
    }
    return StageRun(**(fields | changes))


def ns(timestamp: str) -> int:
    return tracing._ns(timestamp)


def test_a_stage_run_is_a_generation_with_usage_cost_and_times(
    db: Any, spans: InMemorySpanExporter
) -> None:
    tracing.stage_run(db, 3, "research", run(), "c0ffee", "ok", None)
    [span] = spans.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert span.name == "research"
    assert span.start_time == ns("2026-11-19T17:58:44.457953+00:00")
    assert span.end_time == ns("2026-11-19T17:59:38.930955+00:00")
    assert f"{span.context.trace_id:032x}" == tracing.trace_id(3, STARTED)
    assert attrs["langfuse.observation.type"] == "generation"
    assert json.loads(str(attrs["langfuse.observation.usage_details"])) == {
        "input": 43846,
        "input_cache_read": 167936,
        "input_cache_creation": 0,
        "output": 17265,
    }
    assert json.loads(str(attrs["langfuse.observation.cost_details"])) == {"total": 0.034879}
    assert attrs["session.id"] == "issue-3"
    assert attrs["langfuse.trace.name"] == "#3 CSV export misses the header row"
    assert attrs["langfuse.internal.as_root"] is True
    metadata = {k: v for k, v in attrs.items() if k.startswith("langfuse.observation.metadata.")}
    assert metadata["langfuse.observation.metadata.tool_calls"] == "61"
    assert metadata["langfuse.observation.metadata.subagent_tool_calls"] == "46"
    sandbox_session = metadata["langfuse.observation.metadata.sandbox_session_id"]
    assert json.loads(str(sandbox_session)) == str(UUID(int=7))


def test_a_failed_stage_is_an_error(db: Any, spans: InMemorySpanExporter) -> None:
    tracing.stage_run(db, 3, "validate", run(), "c0ffee", "fail", "validation_failed")
    [span] = spans.get_finished_spans()
    attrs = dict(span.attributes or {})
    assert attrs["langfuse.observation.level"] == "ERROR"
    assert attrs["langfuse.observation.status_message"] == "validation_failed"


def test_the_approval_wait_is_a_span_in_the_same_trace(
    db: Any, spans: InMemorySpanExporter
) -> None:
    tracing.stage_run(db, 3, "plan", run(), "c0ffee", "ok", None)
    tracing.approval_wait(db, 3, "2026-11-19T20:45:15Z", "2026-11-19T20:46:00Z")
    plan, wait = spans.get_finished_spans()
    assert wait.name == "awaiting-approval"
    assert dict(wait.attributes or {})["langfuse.observation.type"] == "span"
    assert (wait.start_time, wait.end_time) == (
        ns("2026-11-19T20:45:15Z"),
        ns("2026-11-19T20:46:00Z"),
    )
    assert wait.context.trace_id == plan.context.trace_id


def test_a_run_that_never_started_is_not_traced(db: Any, spans: InMemorySpanExporter) -> None:
    tracing.stage_run(db, 3, "research", run(started_at=None, ended_at=None), "c", "error", "x")
    assert spans.get_finished_spans() == ()


def test_unreachable_langfuse_is_skipped_with_a_warning(
    db: Any, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setattr(config, "LANGFUSE_PUBLIC_KEY", f"pk-test-{uuid.uuid4()}")
    monkeypatch.setattr(config, "LANGFUSE_HOST", "http://127.0.0.1:9")  # nothing listens
    monkeypatch.setattr(tracing, "_client", None)
    with caplog.at_level(logging.WARNING, logger="factory.tracing"):
        tracing.stage_run(db, 3, "research", run(), "c0ffee", "ok", None)  # does not raise
    assert "unreachable" in caplog.text


def generation(name: str, input: int = 10, output: int = 5, cost: float = 0.01) -> Any:
    return SimpleNamespace(
        name=name, usage_details={"input": input, "output": output}, total_cost=cost
    )


@pytest.fixture
def observed(db: Any, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[Any]]:
    """Two stage runs in the store; `check` sees the generations in the returned list."""
    found: list[Any] = []
    calls: list[dict[str, Any]] = []

    def get_many(**kwargs: Any) -> Any:
        calls.append(kwargs)
        return SimpleNamespace(data=found)

    client = SimpleNamespace(api=SimpleNamespace(observations=SimpleNamespace(get_many=get_many)))
    monkeypatch.setattr(tracing, "connect", lambda: (client, None))
    for stage in ("research", "plan"):
        store.record_stage_run(db, 3, stage, run(), "c0ffee", "2026-11-19T20:43:00+00:00")
    yield found
    assert calls[0]["trace_id"] == tracing.trace_id(3, STARTED)
    assert calls[0]["type"] == "GENERATION"


def test_check_passes_with_one_generation_per_stage(observed: list[Any]) -> None:
    observed += [generation("research"), generation("plan")]
    assert tracing.check(3) == []


def test_check_fails_on_a_missing_stage_or_empty_usage(observed: list[Any]) -> None:
    observed += [generation("research", output=0, cost=0.0)]
    assert tracing.check(3) == [
        "research: no input or output tokens ({'input': 10, 'output': 0})",
        "research: no cost",
        "plan: 0 generations, expected 1",
    ]
