"""Langfuse traces: one trace per issue run, one generation per stage run, one span for the
approval wait, all in the Langfuse session `issue-<n>`.

The agent's own model calls happen inside the VM, so a stage is one generation with its
totals. Observations are sent when their run is over, with the times from the session's
event log; the trace id comes from the issue run's start, so the stages of one run share
a trace across processes and days of waiting.

Tracing is best effort: if Langfuse is unreachable it is skipped with a warning. The
SQLite record (`var/factory.db`) stays the source of truth.

`python -m factory.tracing --check issue-<n>` checks the trace of the issue's current run.
"""

import argparse
import datetime
import hashlib
import logging
import sqlite3
import sys
from typing import Any

from langfuse import Langfuse, LangfuseGeneration, LangfuseOtelSpanAttributes, LangfuseSpan
from opentelemetry import trace as otel
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SpanExporter
from opentelemetry.sdk.trace.id_generator import RandomIdGenerator

from factory import config, store
from factory.sandbox import StageRun

log = logging.getLogger("factory.tracing")

_client: tuple[Langfuse, otel.Tracer] | None = None


def connect(exporter: SpanExporter | None = None) -> tuple[Langfuse, otel.Tracer]:
    """The client and a tracer with its own provider, so spans can carry their real start
    time (the SDK's own methods always start now). `exporter` replaces the OTLP one (tests)."""
    global _client
    if _client is None or exporter is not None:
        provider = TracerProvider()
        client = Langfuse(
            public_key=config.LANGFUSE_PUBLIC_KEY,
            secret_key=config.LANGFUSE_SECRET_KEY,
            base_url=config.LANGFUSE_HOST,
            tracer_provider=provider,
            should_export_span=lambda span: True,  # the provider carries only our spans
            span_exporter=exporter,
        )
        _client = (client, provider.get_tracer("factory"))
    return _client


def trace_id(number: int, started_at: str) -> str:
    """The same id for every observation of one run of the issue (32 hex chars)."""
    return hashlib.sha256(f"issue-{number}:{started_at}".encode()).hexdigest()[:32]


def _ns(timestamp: str) -> int:
    when = datetime.datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    return int(when.timestamp() * 1e9)


def _issue(db: sqlite3.Connection, number: int) -> sqlite3.Row:
    row: sqlite3.Row = db.execute(
        "SELECT number, title, started_at FROM issues WHERE number = ?", (number,)
    ).fetchone()
    return row


def _start(tracer: otel.Tracer, issue: sqlite3.Row, name: str, start: str) -> otel.Span:
    """A span in the issue run's trace, as a root observation (its parent is only the id)."""
    parent = otel.NonRecordingSpan(
        otel.SpanContext(
            trace_id=int(trace_id(issue["number"], issue["started_at"]), 16),
            span_id=RandomIdGenerator().generate_span_id(),
            is_remote=True,
            trace_flags=otel.TraceFlags(otel.TraceFlags.SAMPLED),
        )
    )
    attrs = LangfuseOtelSpanAttributes
    return tracer.start_span(
        name,
        context=otel.set_span_in_context(parent),
        start_time=_ns(start),
        attributes={
            attrs.AS_ROOT: True,
            attrs.TRACE_NAME: f"#{issue['number']} {issue['title']}",
            attrs.TRACE_SESSION_ID: f"issue-{issue['number']}",
        },
    )


def _reachable(client: Langfuse) -> bool:
    try:
        return client.auth_check()
    except Exception as exc:
        log.warning("Langfuse at %s is unreachable, tracing skipped: %s", config.LANGFUSE_HOST, exc)
        return False


def stage_run(
    db: sqlite3.Connection,
    number: int,
    stage: str,
    run: StageRun,
    commands_sha: str,
    status: str,
    reason: str | None,
) -> None:
    """One generation for the stage run, with its usage, cost and metrics."""
    if run.started_at is None or run.ended_at is None:
        return  # the run never started: nothing to place on the timeline
    try:
        client, tracer = connect()
        if not _reachable(client):
            return
        span = _start(tracer, _issue(db, number), stage, run.started_at)
        u = run.usage
        LangfuseGeneration(
            otel_span=span,
            langfuse_client=client,
            usage_details={
                "input": u.get("input_tokens", 0),
                "input_cache_read": u.get("cache_read_input_tokens", 0),
                "input_cache_creation": u.get("cache_creation_input_tokens", 0),
                "output": u.get("output_tokens", 0),
            },
            cost_details={"total": run.cost_usd},
            metadata={
                "status": status,
                "reason": reason,
                "run_id": run.run_id,
                "tool_calls": run.tool_calls,
                "subagent_tool_calls": run.subagent_tool_calls,
                "num_turns": run.num_turns,
                "commands_sha": commands_sha,
                "agents_sha": run.agents_sha,
                "sandbox_session_id": str(run.session_id),
            },
            level="DEFAULT" if status == "ok" else "ERROR",
            status_message=reason,
        ).end(end_time=_ns(run.ended_at))
        client.flush()
    except Exception:
        log.warning("issue #%d: could not trace the %s run", number, stage, exc_info=True)


def approval_wait(db: sqlite3.Connection, number: int, start: str, end: str) -> None:
    """The wait from the plan comment to the approver's decision."""
    try:
        client, tracer = connect()
        if not _reachable(client):
            return
        span = _start(tracer, _issue(db, number), "awaiting-approval", start)
        LangfuseSpan(otel_span=span, langfuse_client=client).end(end_time=_ns(end))
        client.flush()
    except Exception:
        log.warning("issue #%d: could not trace the approval wait", number, exc_info=True)


def check(number: int) -> list[str]:
    """Problems with the trace of the issue's current run; empty if it is complete: one
    generation per stage run, each with input and output tokens and a cost."""
    db = store.connect()
    issue = _issue(db, number)
    if issue is None:
        return [f"issue #{number} has no run"]
    runs = store.current_runs(db, number)
    client, _ = connect()
    found: list[Any] = client.api.observations.get_many(
        trace_id=trace_id(number, issue["started_at"]), type="GENERATION", fields="core,basic,usage"
    ).data
    problems = []
    for stage in dict.fromkeys(r["stage"] for r in runs):
        expected = sum(r["stage"] == stage for r in runs)
        generations = [g for g in found if g.name == stage]
        if len(generations) != expected:
            problems.append(f"{stage}: {len(generations)} generations, expected {expected}")
        for g in generations:
            usage = g.usage_details or {}
            if not (usage.get("input", 0) > 0 and usage.get("output", 0) > 0):
                problems.append(f"{stage}: no input or output tokens ({usage})")
            if not (g.total_cost or 0) > 0:
                problems.append(f"{stage}: no cost")
    if not runs:
        problems.append(f"issue #{number} has no stage runs")
    return problems


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m factory.tracing")
    parser.add_argument("--check", metavar="issue-N", required=True)
    args = parser.parse_args()
    number = int(args.check.removeprefix("issue-"))
    problems = check(number)
    for p in problems:
        print(p)
    print(f"issue #{number}: trace {'incomplete' if problems else 'complete'}")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
