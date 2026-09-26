"""Tests for herness.harness.tracing: Tracer, TraceType and the trace helpers (U05-69, U05-70)."""

from __future__ import annotations

import datetime
import json
import threading
from collections.abc import Callable, Iterator
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from herness.core import redact as r
from herness.core.errors import ConfigError
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    LoopState,
    Message,
    ReasoningPart,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolCall,
    ToolErrorInfo,
    ToolResult,
    TraceEmitter,
    Usage,
)
from herness.harness import tracing as t
from herness.harness.llm.settings import TraceSettings

pytestmark = pytest.mark.unit

RUN_ID = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
COMMON = ("ts", "type", "run_id", "task_id", "build_id", "role", "step", "span_id")
COMMON_ALL = (*COMMON, "parent_span_id")
SENTINEL_EMAIL = "sentinel.person@example.org"
ALL_ONE = {"eval": 1.0, "chat": 1.0, "review": 1.0}


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and one directory name (no config needed)."""
    directory = NameDirectory.from_files(None, ("Jane Doe",), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


def _tracer(tmp_path: Path, rates: dict[str, float] | None = None, **kw: Any) -> t.Tracer:
    settings = TraceSettings(payload_sample_rate=rates or ALL_ONE)
    return t.Tracer(
        RUN_ID,
        build_id="20260925-101500-ABCDEF",
        run_kind="eval",
        traces_dir=tmp_path / "traces",
        settings=settings,
        **kw,
    )


def _lines(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "traces" / f"{RUN_ID}.jsonl"
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return [json.loads(line) for line in text.splitlines()]


@pytest.fixture
def paused(monkeypatch: pytest.MonkeyPatch) -> Iterator[threading.Event]:
    """Hold every new writer thread before its first `get` until the event is set."""
    gate = threading.Event()
    original: Callable[[t._Core], None] = t._Core._run

    def held(self: t._Core) -> None:
        gate.wait(10)
        original(self)

    monkeypatch.setattr(t._Core, "_run", held)
    yield gate
    gate.set()


def _request(opaque: bool = True) -> LLMRequest:
    reasoning = ReasoningPart(text="thinking", provider="anthropic", opaque={"sig": "SIGNATURE"})
    parts: list[Any] = [TextPart(text="hello")]
    if opaque:
        parts.append(reasoning)
    return LLMRequest(
        client="local-30b",
        system=[SystemBlock(text="be terse")],
        messages=[
            Message(role="user", parts=[TextPart(text="question")]),
            Message(role="assistant", parts=parts),
        ],
        response_schema={"type": "object"},
        response_schema_name="Answer",
        max_output_tokens=100,
        effort="high",
        thinking="on",
        timeout_s=10.0,
        metadata=RequestMeta(
            run_id=RUN_ID,
            task_id="task_1",
            role="analyst",
            model_role="main",
            step=3,
            request_key="task_1:3:main",
        ),
    )


def _response() -> LLMResponse:
    return LLMResponse(
        text="answer",
        tool_calls=[ToolCall(id="c1", name="run_sql", arguments={"sql": "SELECT 1"})],
        parsed=None,
        reasoning=[ReasoningPart(text="r", provider="anthropic", opaque={"sig": "SIGNATURE"})],
        stop_reason="tool_use",
        raw_stop_reason="tool_use",
        refusal_category=None,
        usage=Usage(input_tokens=10, output_tokens=5, reasoning_tokens=2),
        cost_usd=Decimal("0.0123"),
        client="local-30b",
        model="local-30b-model",
        provider="openai_compat",
        latency_ms=42,
        request_id="req-1",
    )


# --- UT05-43: one valid JSON line per type with the common fields ------------------------


def test_ut05_43_each_type_writes_one_json_line_with_common_fields(tmp_path: Path) -> None:
    """UT05-43 emit each TraceType: one valid JSON object line each with the common fields."""
    tracer = _tracer(tmp_path)
    assert isinstance(tracer, TraceEmitter)
    spans = [tracer.emit(kind, step=1, detail=kind.value) for kind in t.TraceType]
    tracer.close()
    lines = _lines(tmp_path)
    assert [line["type"] for line in lines] == [kind.value for kind in t.TraceType]
    assert [line["span_id"] for line in lines] == spans
    for line in lines:
        assert set(COMMON_ALL) <= set(line)
        assert line["run_id"] == RUN_ID
        assert line["build_id"] == "20260925-101500-ABCDEF"
        assert len(line["ts"]) == 27
        assert line["ts"].endswith("Z")
        assert line["task_id"] is None
        assert line["role"] is None
        assert line["step"] == 1
    assert len(t.TraceType) == 10
    assert tracer.health() == {"status": "ok"}


def test_ut05_43_unknown_type_and_reserved_field_raise_config_error(tmp_path: Path) -> None:
    """UT05-43 an unknown type or a field named like a common field is a ConfigError."""
    tracer = _tracer(tmp_path)
    with pytest.raises(ConfigError, match=r"^unknown trace type bogus$"):
        tracer.emit("bogus")
    with pytest.raises(ConfigError, match=r"^reserved trace field span_id$"):
        tracer.emit("retry", span_id="x")
    tracer.close()
    assert not _lines(tmp_path)


def test_ut05_43_fields_are_json_safe(tmp_path: Path) -> None:
    """UT05-43 Decimal -> str, datetime -> ISO Z, pydantic -> JSON dump, NaN and other -> str."""
    tracer = _tracer(tmp_path)
    when = datetime.datetime(2026, 9, 26, 1, 2, 3, tzinfo=datetime.UTC)
    tracer.emit(
        "budget",
        kind="warn",
        used={"cost_usd": Decimal("1.50"), "tags": ("a", "b"), "ids": {3}},
        at=when,
        naive=datetime.datetime(2026, 1, 1),  # noqa: DTZ001 - exercising naive input
        day=datetime.date(2026, 1, 2),
        usage=Usage(input_tokens=1),
        nan=float("nan"),
        path=Path("a"),
        n=3,
        ok=True,
    )
    tracer.close()
    (line,) = _lines(tmp_path)
    assert line["used"] == {"cost_usd": "1.50", "tags": ["a", "b"], "ids": [3]}
    assert line["at"] == "2026-09-26T01:02:03Z"
    assert line["naive"] == "2026-01-01T00:00:00"
    assert line["day"] == "2026-01-02"
    assert line["usage"]["input_tokens"] == 1
    assert line["nan"] == "NaN"
    assert line["path"] == "a"
    assert line["n"] == 3
    assert line["ok"] is True


def test_ut05_43_bind_views_share_the_writer(tmp_path: Path) -> None:
    """UT05-43 bound views default task_id and role, share the file; view close is a no-op."""
    tracer = _tracer(tmp_path)
    view = tracer.bind(task_id="task_7", role="analyst")
    parent = tracer.emit("spawn_decision")
    view.emit("tool_call", parent_span_id=parent)
    view.emit("tool_call", task_id="task_8", role="critic")
    view.close()
    assert tracer.health() == {"status": "ok"}
    tracer.close()
    tracer.close()  # idempotent
    root, bound, override = _lines(tmp_path)
    assert (root["task_id"], root["role"]) == (None, None)
    assert (bound["task_id"], bound["role"], bound["parent_span_id"]) == (
        "task_7",
        "analyst",
        parent,
    )
    assert (override["task_id"], override["role"]) == ("task_8", "critic")


def test_ut05_43_llm_call_and_tool_call_fields(tmp_path: Path, test_redactor: r.Redactor) -> None:
    """UT05-43 llm_call_fields / tool_call_fields give the design §5.7 field sets."""
    fields, payload = t.llm_call_fields(_request(), _response(), prompt_hash="ph", gate_wait_ms=7)
    assert fields == {
        "client": "local-30b",
        "model": "local-30b-model",
        "provider": "openai_compat",
        "prompt_hash": "ph",
        "n_messages": 2,
        "n_tools": 0,
        "response_schema_name": "Answer",
        "thinking": "on",
        "effort": "high",
        "stop_reason": "tool_use",
        "refusal_category": None,
        "usage": {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "reasoning_tokens": 2,
        },
        "cost_usd": "0.012300",
        "latency_ms": 42,
        "request_id": "req-1",
        "tool_call_names": ["run_sql"],
        "gate_wait_ms": 7,
    }
    assert set(payload) == {"system", "messages", "response_text", "tool_calls"}
    assert payload["system"] == ["be terse"]
    assert payload["response_text"] == "answer"
    assert "SIGNATURE" not in json.dumps(payload)
    call = ToolCall(id="c1", name="run_sql", arguments={"sql": "SELECT 1"})
    err = ToolErrorInfo(type="QueryError", message="bad", hint=None)
    result = ToolResult(ok=False, content="x", error=err, duration_ms=5, row_count=0)
    tfields, tpayload = t.tool_call_fields(call, result)
    assert tfields == {
        "tool": "run_sql",
        "args_hash": LoopState.call_signature("run_sql", {"sql": "SELECT 1"}),
        "ok": False,
        "error_type": "QueryError",
        "query_ids": [],
        "row_count": 0,
        "truncated": False,
        "duration_ms": 5,
    }
    assert tpayload == {"args": {"sql": "SELECT 1"}}
    ok_fields, _ = t.tool_call_fields(call, ToolResult(ok=True, content="y"))
    assert ok_fields["error_type"] is None
    tracer = _tracer(tmp_path)
    tracer.emit("llm_call", payload=payload, **fields)
    tracer.emit("tool_call", payload=tpayload, **tfields)
    tracer.close()
    llm, tool = _lines(tmp_path)
    assert llm["payload"]["messages"][1]["parts"][1] == {
        "type": "reasoning",
        "text": "thinking",
        "provider": "anthropic",
    }
    assert tool["payload"] == {"args": {"sql": "SELECT 1"}}


# --- UT05-44: deterministic hash sampling ------------------------------------------------


def test_ut05_44_sampling_is_deterministic_and_near_the_rate(tmp_path: Path) -> None:
    """UT05-44 rates 0.1: is_sampled is deterministic; ~10 % of 10,000 ids (± 1.5 %)."""
    tracer = _tracer(tmp_path, {"eval": 0.1, "chat": 0.1, "review": 0.1})
    ids = [f"task_{i:05d}" for i in range(10_000)]
    first = [tracer.is_sampled(i) for i in ids]
    assert first == [tracer.is_sampled(i) for i in ids]
    assert 0.085 <= sum(first) / len(ids) <= 0.115
    assert tracer.is_sampled(None) == tracer.is_sampled(RUN_ID)
    tracer.close()


def test_ut05_44_unsampled_task_keeps_no_payload(tmp_path: Path) -> None:
    """UT05-44 rate 0.0: payloads are never written, the event is."""
    tracer = _tracer(tmp_path, {"eval": 0.0, "chat": 0.0, "review": 0.0})
    tracer.emit("tool_call", task_id="task_1", payload={"args": {"a": 1}})
    tracer.close()
    (line,) = _lines(tmp_path)
    assert "payload" not in line
    assert "payload_dropped" not in line


# --- UT05-45: payload redaction, opaque removal and cut -----------------------------------


def test_ut05_45_payload_redacted_opaque_removed_and_cut(
    tmp_path: Path, test_redactor: r.Redactor
) -> None:
    """UT05-45 sentinel email redacted, opaque absent at every depth, 50,000 chars cut to 20,000."""
    tracer = _tracer(tmp_path)
    payload = {
        "messages": [
            {"text": f"mail {SENTINEL_EMAIL} now", "opaque": {"sig": "SIG"}},
            {"parts": [{"deep": {"opaque": "x", "keep": 1}}]},
            {"text": "a" * 50_000},
        ],
        "none": None,
    }
    tracer.emit("llm_call", payload=payload)
    tracer.close()
    (line,) = _lines(tmp_path)
    text = json.dumps(line)
    assert SENTINEL_EMAIL not in text
    assert '"opaque"' not in text
    first, second, third = line["payload"]["messages"]
    assert first["text"].startswith("mail [EMAIL_")
    assert second == {"parts": [{"deep": {"keep": 1}}]}
    assert len(third["text"]) == 20_000
    assert line["payload"]["none"] is None


def test_ut05_45_redaction_failure_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-45 redact_text None -> string omitted; redactor raising -> payload dropped."""
    tracer = _tracer(tmp_path)
    monkeypatch.setattr(t, "redact_text", lambda _text: None)
    tracer.emit("llm_call", payload={"m": "secret text"})

    def boom(_text: str | None) -> str | None:
        msg = "secret not found: redact.hmac_key"
        raise ConfigError(msg)

    monkeypatch.setattr(t, "redact_text", boom)
    tracer.emit("llm_call", payload={"m": "secret text"})
    tracer.close()
    none_line, failed_line = _lines(tmp_path)
    assert none_line["payload"] == {"m": None}
    assert "payload" not in failed_line
    assert failed_line["payload_dropped"] is True


# --- UT05-46: bounded queue, payload first, then events -----------------------------------


def test_ut05_46_payloads_then_events_dropped_and_reported(
    tmp_path: Path, paused: threading.Event, test_redactor: r.Redactor
) -> None:
    """UT05-46 queue_max 10, writer paused, 20 emits: payload dropped from 8, events at 10."""
    tracer = _tracer(tmp_path, queue_max=10)
    for i in range(20):
        tracer.emit("tool_call", step=i, payload={"args": {"i": i}})
    assert tracer.health() == {"status": "degraded"}
    paused.set()
    tracer.close()
    lines = _lines(tmp_path)
    events = [line for line in lines if line["type"] == "tool_call"]
    assert [e["step"] for e in events] == list(range(10))
    assert all("payload" in e for e in events[:8])
    assert all(e.get("payload_dropped") is True and "payload" not in e for e in events[8:])
    (budget,) = [line for line in lines if line["type"] == "budget"]
    assert budget["kind"] == "dropped_events"
    assert (budget["used"], budget["limit"]) == ({}, {})
    assert budget["message"] == "dropped 10 trace events"
    assert lines.index(budget) >= 1  # reported by the writer after it resumed


def test_ut05_46_emit_after_close_is_counted_dropped(tmp_path: Path) -> None:
    """UT05-46 emits after close are dropped and counted; health reports degraded."""
    tracer = _tracer(tmp_path)
    tracer.close()
    span = tracer.emit("retry")
    assert len(span) == 26
    assert tracer.health() == {"status": "degraded"}
    assert not _lines(tmp_path)


def test_ut05_46_io_error_is_logged_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT05-46 an I/O error in the writer logs harness.trace.write_failed and keeps draining."""
    calls = {"n": 0}
    original = Path.open

    def flaky(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.suffix == ".jsonl" and calls["n"] < 1:
            calls["n"] += 1
            msg = "disk full"
            raise OSError(msg)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", flaky)
    tracer = _tracer(tmp_path)
    tracer.emit("retry", attempt=1)
    tracer.emit("retry", attempt=2)
    tracer.emit("retry", attempt=3)
    tracer.close()
    lines = _lines(tmp_path)
    assert [line.get("attempt") for line in lines] == [None, 2, 3]
    assert lines[0]["message"] == "dropped 1 trace events"
    assert "harness.trace.write_failed" in "".join(capsys.readouterr())


def test_ut05_46_scrub_failure_drops_the_event(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-46 a failed secret scrub (fail-closed replacement event) drops the event."""
    monkeypatch.setattr(t, "scrub_secrets", lambda *_a: {"event": "log.scrub.failed"})
    tracer = _tracer(tmp_path)
    tracer.emit("retry")
    tracer.close()
    assert not _lines(tmp_path)
    assert tracer.health() == {"status": "degraded"}


def test_ut05_46_dead_writer_reports_down(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-46 health is down when the writer thread died while the tracer is open."""

    def die(self: t._Core) -> None:
        return None

    monkeypatch.setattr(t._Core, "_run", die)
    tracer = _tracer(tmp_path)
    tracer._core.thread.join(5)  # type: ignore[union-attr]
    assert tracer.health() == {"status": "down"}


def test_ut05_46_close_timeout_logs_warning(
    tmp_path: Path, paused: threading.Event, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT05-46 close with a stuck writer logs WARNING harness.trace.close_timeout."""
    tracer = _tracer(tmp_path, queue_max=1)
    tracer.emit("retry")
    tracer.close(timeout_s=0.05)
    assert "harness.trace.close_timeout" in "".join(capsys.readouterr())
    paused.set()
    tracer._core.thread.join(5)  # type: ignore[union-attr]


def test_ut05_46_periodic_flush_without_close(tmp_path: Path) -> None:
    """UT05-46 the writer flushes after flush_interval_s without waiting for close."""
    tracer = _tracer(tmp_path, flush_interval_s=0.01)
    tracer.emit("retry")
    path = tmp_path / "traces" / f"{RUN_ID}.jsonl"
    done = threading.Event()
    for _ in range(500):
        if path.exists() and path.read_text(encoding="utf-8").endswith("\n"):
            done.set()
            break
        done.wait(0.01)
    assert done.is_set()
    tracer.close()


# --- Constructor, for_run, null ------------------------------------------------------------


@pytest.mark.parametrize("run_id", ["run_bad", "../run_01J8ZZZZZZZZZZZZZZZZZZZZZZ", "x" * 30])
def test_ut05_43_bad_run_id_is_config_error(tmp_path: Path, run_id: str) -> None:
    """UT05-43 a run_id not matching run_<ULID> is a ConfigError (path safety)."""
    with pytest.raises(ConfigError, match=r"^invalid trace run_id$"):
        t.Tracer(
            run_id,
            build_id=None,
            run_kind="eval",
            traces_dir=tmp_path,
            settings=TraceSettings(),
        )


def test_ut05_43_bad_arguments_and_unwritable_dir(tmp_path: Path) -> None:
    """UT05-43 bad queue_max, flush interval or directory raise ConfigError."""
    base: dict[str, Any] = {"build_id": None, "run_kind": "eval", "settings": TraceSettings()}
    with pytest.raises(ConfigError, match=r"^trace queue_max and flush_interval_s must be > 0$"):
        t.Tracer(RUN_ID, **base, traces_dir=tmp_path, queue_max=0)
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    with pytest.raises(ConfigError, match=r"^trace directory cannot be created$"):
        t.Tracer(RUN_ID, **base, traces_dir=blocker / "traces")


def test_ut05_43_null_tracer_discards(tmp_path: Path) -> None:
    """UT05-43 Tracer.null() returns span ids, writes nothing and still rejects bad types."""
    tracer = t.Tracer.null()
    assert len(tracer.emit("retry", payload={"a": 1})) == 26
    with pytest.raises(ConfigError):
        tracer.emit("nope")
    assert tracer.health() == {"status": "ok"}
    assert tracer.task_id is None
    tracer.close()


def test_ut05_43_for_run_reads_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-43 for_run writes under paths.data/traces with models.harness.trace settings."""

    class _Cfg:
        class paths:  # noqa: N801 - attribute-shaped stand-in
            data = tmp_path / "data"

        class models:  # noqa: N801
            class harness:  # noqa: N801
                trace = TraceSettings(payload_sample_rate=ALL_ONE)

    monkeypatch.setattr(t, "get_config", lambda: _Cfg)
    tracer = t.Tracer.for_run(RUN_ID, build_id=None, run_kind="chat")
    assert tracer.is_sampled("task_1")
    tracer.emit("compaction")
    tracer.close()
    assert (tmp_path / "data" / "traces" / f"{RUN_ID}.jsonl").is_file()


# --- UT05-130 (tracer part): run_id / task_id properties (R-66) ----------------------------


def test_ut05_130_tracer_properties_root_and_bound(tmp_path: Path) -> None:
    """UT05-130 tracer.run_id and task_id equal the bound values; root task_id is None."""
    tracer = _tracer(tmp_path)
    view = tracer.bind(task_id="task_9", role="analyst")
    assert (tracer.run_id, tracer.task_id) == (RUN_ID, None)
    assert (view.run_id, view.task_id) == (RUN_ID, "task_9")
    tracer.close()
