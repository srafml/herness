"""The only JSONL trace writer (U05-69, U05-70; design 05 §5.7, spec 00 §8).

``emit`` never blocks: at 80 % of the queue the payload is dropped first, when full the event is
dropped and counted, and the writer thread reports the count in a ``dropped_events`` budget event.
Payloads of sampled tasks are redacted, scrubbed and cut per string, never with an ``opaque`` key;
the writer scrubs every event again before it appends the line (TH05-14, TH05-19).
"""

from __future__ import annotations

import contextlib
import hashlib
import queue
import re
import threading
from enum import StrEnum
from pathlib import Path
from typing import IO, Final, Literal

from pydantic import BaseModel
from pydantic_core import to_json, to_jsonable_python

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.ids import new_ulid
from herness.core.logging import get_logger
from herness.core.redact import redact_text
from herness.core.secrets import scrub_secrets
from herness.core.types import LLMRequest, LLMResponse, LoopState, ToolCall, ToolResult
from herness.harness.llm.settings import TraceSettings

__all__ = ["TraceType", "Tracer", "llm_call_fields", "tool_call_fields"]


# The trace event types of design §5.7; each value equals the member name.
TraceType = StrEnum(
    "TraceType",
    "llm_call tool_call retry repair fallback guard_stop budget compaction verifier_verdict "
    "spawn_decision",
)
type _Kind = Literal["eval", "chat", "review"]
type _Event = dict[str, object]
_log = get_logger("harness.trace")
_RUN_ID_RE: Final = re.compile(r"run_[0-9A-HJKMNP-TV-Z]{26}", re.ASCII)
_TYPES: Final = frozenset(TraceType)
_COMMON: Final = ("ts", "type", "run_id", "task_id", "build_id", "role", "step", "span_id")
_RESERVED: Final = frozenset((*_COMMON, "parent_span_id", "payload", "payload_dropped"))
_DEGRADED_WINDOW_S: Final = 60.0
_STOP: Final = object()
_LLM_ATTRS: Final = ("client", "model", "provider")
_LLM_RESP_ATTRS: Final = ("stop_reason", "refusal_category", "latency_ms", "request_id")
_TOOL_RESULT_ATTRS: Final = ("row_count", "truncated", "duration_ms")


def _clean_text(text: str, max_chars: int) -> str | None:
    """Redact, cut, then scrub one payload string; ``None`` when either step fails closed."""
    redacted = redact_text(text)
    if redacted is None:
        return None
    scrubbed = scrub_secrets(None, "trace", {"t": redacted[:max_chars]}).get("t")
    return scrubbed if isinstance(scrubbed, str) else None


def _clean_payload(value: object, max_chars: int) -> object:
    """JSON-safe payload with every string cleaned and every ``opaque`` key removed."""
    if isinstance(value, str):
        return _clean_text(value, max_chars)
    if isinstance(value, dict):
        return {k: _clean_payload(v, max_chars) for k, v in value.items() if k != "opaque"}
    if isinstance(value, list):
        return [_clean_payload(v, max_chars) for v in value]
    return value


class _Core:
    """State shared by a root ``Tracer`` and its views: queue, drop counter, writer and file."""

    def __init__(self, run_id: str, build_id: str | None, path: Path | None) -> None:
        self.run_id, self.build_id, self.path = run_id, build_id, path
        self.rate, self.max_chars, self.flush_interval_s = 0.0, 0, 1.0
        self.queue: queue.Queue[object] = queue.Queue(maxsize=1)
        self.payload_limit = 1
        self.lock = threading.Lock()
        self.dropped, self.closed = 0, False
        self.last_drop: float | None = None
        self.fh: IO[bytes] | None = None
        self.failing = self.dirty = False
        self.last_flush = clock.monotonic()
        self.thread: threading.Thread | None = None

    def start(self, settings: TraceSettings, kind: _Kind, queue_max: int, interval: float) -> None:
        self.rate = settings.payload_sample_rate[kind]
        self.max_chars, self.flush_interval_s = settings.max_payload_chars, interval
        self.queue = queue.Queue(maxsize=queue_max)
        self.payload_limit = -(-queue_max * 4 // 5)  # ceil(80 % of queue_max)
        self.thread = threading.Thread(target=self._run, name="herness-trace", daemon=True)
        self.thread.start()

    def drop(self, n: int = 1) -> None:
        with self.lock:
            self.dropped += n
            self.last_drop = clock.monotonic()

    def event(self, kind: str, task_id: str | None, role: str | None, step: int | None) -> _Event:
        values = (clock.format_utc(clock.now()), kind, self.run_id, task_id, self.build_id, role)
        return dict(zip(_COMMON, (*values, step, new_ulid()), strict=True))

    def _write(self, event: _Event) -> None:
        scrubbed = scrub_secrets(None, "trace", event)
        if "type" not in scrubbed:  # the scrubber failed closed
            self._failed("ScrubFailed")
            return
        line = to_json(scrubbed, inf_nan_mode="strings", fallback=str) + b"\n"
        try:
            if self.fh is None:
                assert self.path is not None  # noqa: S101 - only a root with a file writes
                self.fh = self.path.open("ab")
            self.fh.write(line)
        except (OSError, ValueError) as exc:
            self._failed(type(exc).__name__)
            return
        self.dirty, self.failing = True, False

    def _failed(self, error_type: str) -> None:
        self.drop()
        if not self.failing:  # one ERROR line per failure streak, never event text
            _log.error("harness.trace.write_failed", run_id=self.run_id, error_type=error_type)
        self.failing = True

    def _flush(self, *, force: bool = False) -> None:
        now = clock.monotonic()
        if self.fh is None or not self.dirty:
            return
        if force or now - self.last_flush >= self.flush_interval_s:
            self.dirty, self.last_flush = False, now
            try:
                self.fh.flush()
            except OSError as exc:
                self._failed(type(exc).__name__)

    def _report_drops(self) -> None:
        with self.lock:
            n, self.dropped = self.dropped, 0
        if n > 0:
            report = self.event("budget", None, None, None)
            report.update(parent_span_id=None, kind="dropped_events", used={}, limit={})
            report["message"] = f"dropped {n} trace events"
            self._write(report)

    def _run(self) -> None:
        try:
            while True:
                item = None
                with contextlib.suppress(queue.Empty):
                    item = self.queue.get(timeout=self.flush_interval_s)
                if item is _STOP or (item is None and self.closed):
                    break
                if isinstance(item, dict):
                    self._write(item)
                self._report_drops()
                self._flush()
        finally:
            late = 0
            with contextlib.suppress(queue.Empty):  # events that raced past close: counted
                while True:
                    late += self.queue.get_nowait() is not _STOP
            if late:
                self.drop(late)
            self._report_drops()
            self._flush(force=True)
            if self.fh is not None:
                self.fh.close()

    def close(self, timeout_s: float) -> None:
        with self.lock:
            was_closed, self.closed = self.closed, True
        if was_closed or self.thread is None:
            return
        deadline = clock.monotonic() + timeout_s
        with contextlib.suppress(queue.Full):  # a stuck writer: the join below times out
            self.queue.put(_STOP, timeout=timeout_s)
        self.thread.join(max(0.0, deadline - clock.monotonic()))
        if self.thread.is_alive():
            _log.warning("harness.trace.close_timeout", run_id=self.run_id, timeout_s=timeout_s)


class Tracer:
    """JSONL trace writer implementing ``TraceEmitter`` (U05-69, R-66)."""

    def __init__(  # noqa: PLR0913 - signature fixed by U05-69
        self,
        run_id: str,
        *,
        build_id: str | None,
        run_kind: _Kind,
        traces_dir: Path,
        settings: TraceSettings,
        queue_max: int = 10_000,
        flush_interval_s: float = 1.0,
    ) -> None:
        if _RUN_ID_RE.fullmatch(run_id) is None:
            msg = "invalid trace run_id"
            raise ConfigError(msg)
        if queue_max < 1 or not flush_interval_s > 0:
            msg = "trace queue_max and flush_interval_s must be > 0"
            raise ConfigError(msg)
        try:
            traces_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            msg = "trace directory cannot be created"
            raise ConfigError(msg, error_type=type(exc).__name__) from exc
        self._set(_Core(run_id, build_id, traces_dir / f"{run_id}.jsonl"), None, None, view=False)
        self._core.start(settings, run_kind, queue_max, flush_interval_s)

    @classmethod
    def for_run(cls, run_id: str, *, build_id: str | None, run_kind: _Kind) -> Tracer:
        """A tracer writing to ``<paths.data>/traces`` with ``models.harness.trace`` settings."""
        cfg = get_config()
        traces_dir, settings = Path(cfg.paths.data) / "traces", cfg.models.harness.trace
        return cls(
            run_id, build_id=build_id, run_kind=run_kind, traces_dir=traces_dir, settings=settings
        )

    @classmethod
    def null(cls) -> Tracer:
        """A tracer that discards every event (code outside runs, tests)."""
        return cls.__new__(cls)._set(_Core("run_" + "0" * 26, None, None), None, None, view=False)

    def _set(self, core: _Core, task_id: str | None, role: str | None, *, view: bool) -> Tracer:
        self._core, self._task_id, self._role, self._view = core, task_id, role, view
        return self

    @property
    def run_id(self) -> str:
        return self._core.run_id

    @property
    def task_id(self) -> str | None:
        """The bound task of a view; ``None`` on the root (R-66)."""
        return self._task_id

    def bind(self, *, task_id: str, role: str) -> Tracer:
        """A view sharing the queue and writer, with default ``task_id`` and ``role``."""
        return Tracer.__new__(Tracer)._set(self._core, task_id, role, view=True)

    def emit(
        self,
        type: str,  # noqa: A002 - name fixed by U05-07
        /,
        *,
        task_id: str | None = None,
        role: str | None = None,
        step: int | None = None,
        parent_span_id: str | None = None,
        payload: object | None = None,
        **fields: object,
    ) -> str:
        """Enqueue one event without blocking and return its new ``span_id``."""
        if type not in _TYPES:
            msg = f"unknown trace type {type}"
            raise ConfigError(msg)
        core = self._core
        if core.thread is None:  # the null tracer
            return new_ulid()
        for name in _RESERVED.intersection(fields):
            msg = f"reserved trace field {name}"
            raise ConfigError(msg)
        task = self._task_id if task_id is None else task_id
        event = core.event(str(type), task, self._role if role is None else role, step)
        event["parent_span_id"] = parent_span_id
        event.update(to_jsonable_python(fields, fallback=str))
        span_id = str(event["span_id"])
        if core.closed:
            core.drop()
            return span_id
        if payload is not None and self.is_sampled(task):
            self._attach_payload(event, payload)
        try:
            core.queue.put_nowait(event)
        except queue.Full:
            core.drop()
        return span_id

    def _attach_payload(self, event: _Event, payload: object) -> None:
        core = self._core
        if core.queue.qsize() >= core.payload_limit:
            event["payload_dropped"] = True
            return
        try:
            event["payload"] = _clean_payload(
                to_jsonable_python(payload, fallback=str), core.max_chars
            )
        except Exception as exc:  # noqa: BLE001 - redaction must fail closed, never raise
            _log.warning("harness.trace.payload_failed", error_type=type(exc).__name__)
            event["payload_dropped"] = True

    def is_sampled(self, task_id: str | None) -> bool:
        """Hash-based per-task payload sampling: the same answer for every event of a task."""
        key = self._core.run_id if task_id is None else task_id
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()
        return int(digest[:8], 16) / 2**32 < self._core.rate

    def close(self, timeout_s: float = 5.0) -> None:
        """Drain, flush and close the file (root only; a bound view's close is a no-op)."""
        if not self._view:
            self._core.close(timeout_s)

    def health(self) -> dict[str, str]:
        """``down`` if the writer died while open, ``degraded`` after a drop in 60 s, else ok."""
        core = self._core
        if core.thread is not None and not core.closed and not core.thread.is_alive():
            return {"status": "down"}
        last = core.last_drop
        if last is not None and clock.monotonic() - last < _DEGRADED_WINDOW_S:
            return {"status": "degraded"}
        return {"status": "ok"}


def _part(part: BaseModel) -> object:
    return part.model_dump(mode="json", exclude={"opaque"})


def llm_call_fields(
    req: LLMRequest, resp: LLMResponse, *, prompt_hash: str, gate_wait_ms: int
) -> tuple[dict[str, object], dict[str, object]]:
    """The ``llm_call`` fields and payload of design §5.7 (pure; U05-70)."""
    fields: dict[str, object] = {name: getattr(resp, name) for name in _LLM_ATTRS}
    fields.update(prompt_hash=prompt_hash, n_messages=len(req.messages), n_tools=len(req.tools))
    fields.update(response_schema_name=req.response_schema_name, thinking=req.thinking)
    fields["effort"] = req.effort
    fields.update({name: getattr(resp, name) for name in _LLM_RESP_ATTRS})
    fields.update(usage=resp.usage.model_dump(mode="json"), cost_usd=str(resp.cost_usd))
    fields["tool_call_names"] = [call.name for call in resp.tool_calls]
    fields["gate_wait_ms"] = gate_wait_ms
    messages = [  # parts without ReasoningPart.opaque (``emit`` also drops it at any depth)
        {"role": m.role, "kind": m.kind, "parts": [_part(p) for p in m.parts]} for m in req.messages
    ]
    payload: dict[str, object] = {
        "system": [block.text for block in req.system],
        "messages": messages,
        "response_text": resp.text,
        "tool_calls": [call.model_dump(mode="json") for call in resp.tool_calls],
    }
    return fields, payload


def tool_call_fields(
    call: ToolCall, result: ToolResult
) -> tuple[dict[str, object], dict[str, object]]:
    """The ``tool_call`` fields and payload of design §5.7 (pure; U05-70)."""
    fields: dict[str, object] = {"tool": call.name}
    fields["args_hash"] = LoopState.call_signature(call.name, call.arguments)
    fields["ok"] = result.ok
    fields["error_type"] = None if result.error is None else result.error.type
    fields["query_ids"] = list(result.query_ids)
    fields.update({name: getattr(result, name) for name in _TOOL_RESULT_ATTRS})
    return fields, {"args": dict(call.arguments)}
