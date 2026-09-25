"""Private structlog processors and daily JSONL handler (impl 00 §3.4); see herness.core.logging."""

from __future__ import annotations

import contextlib
import datetime
import decimal
import enum
import json
import logging
import math
import os
import pathlib
import re
import sys
from collections.abc import Mapping
from typing import Final, TextIO

from structlog.typing import EventDict, Processor

from herness.core import time as clock
from herness.core.errors import SchemaViolation

REQUIRED_KEYS: Final = ("ts", "level", "event", "component")
CONTEXT_ID_KEYS: Final = ("run_id", "task_id", "job_id", "build_id")
_SENSITIVE_NAMES: Final = (
    "password passwd secret token api_key apikey authorization cookie set_cookie "
    "private_key client_secret access_token refresh_token"
)
_FREE_TEXT_NAMES: Final = (
    "prompt prompts completion messages text ticket_text description short_description "
    "close_notes comments summary body content payload raw"
)
SECRET_KEYS: Final[frozenset[str]] = frozenset(_SENSITIVE_NAMES.split())
TEXT_KEYS: Final[frozenset[str]] = frozenset(_FREE_TEXT_NAMES.split())
MAX_FIELD_CHARS: Final = 2000
MAX_LINE_BYTES: Final = 16384
MAX_DEPTH: Final = 4
EVENT_NAME_RE: Final = re.compile(r"[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*){2,}")
FILE_RETRY_S: Final = 60.0
LOG_FILE_PREFIX: Final = "herness-"
OMITTED: Final = "[omitted]"
_SECRET_SUFFIXES: Final = ("_password", "_secret", "_token", "_api_key")
_NEVER_GUARDED: Final = frozenset({"event", "component", "level", "ts"})
_META_KEYS: Final = frozenset({"_record", "_from_structlog"})
_FIRST_KEYS: Final = ("ts", "level", "event", "component", "pid", *CONTEXT_ID_KEYS)
_KEPT_KEYS: Final = frozenset({*_FIRST_KEYS, "dropped_fields"})
_TRUNCATED: Final = "…[truncated]"
_EVENT_CUT: Final = 200
_MASK: Final = "**********"


def _is_secret_key(lowered: str) -> bool:
    return lowered in SECRET_KEYS or lowered.endswith(_SECRET_SUFFIXES)


def add_component(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Fill ``component`` from the standard-library logger name when it is missing."""
    if "component" in event_dict:
        return event_dict
    record = event_dict.get("_record")
    if not isinstance(record, logging.LogRecord):
        event_dict["component"] = "unknown"
    elif record.name.startswith("herness."):
        event_dict["component"] = record.name[len("herness.") :]
    else:
        event_dict["component"] = "ext." + record.name.split(".", 1)[0]
    return event_dict


def add_timestamp(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Add ``ts`` (fixed-width UTC text, follows FakeClock) and ``pid``."""
    event_dict["ts"] = clock.format_utc(clock.now())
    event_dict["pid"] = os.getpid()
    return event_dict


def _safe_str(value: object) -> str:
    try:
        return str(value)
    except (ValueError, TypeError, RecursionError):
        return "<unprintable " + type(value).__name__ + ">"


def _is_pydantic_secret(value: object) -> bool:
    cls = type(value)
    return cls.__name__ in {"SecretStr", "SecretBytes"} and cls.__module__.startswith("pydantic")


def _normalize_leaf(value: object, depth: int) -> object:
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            result: object = value.isoformat() + " naive"
        else:
            result = clock.format_utc(value)
    elif isinstance(value, decimal.Decimal):
        result = str(value)
    elif isinstance(value, pathlib.PurePath):
        result = value.as_posix()
    elif isinstance(value, enum.Enum):
        result = _normalize(value.value, depth)
    elif _is_pydantic_secret(value):
        result = _MASK
    elif isinstance(value, BaseException):
        result = type(value).__name__ + ": " + _safe_str(value)
    else:
        result = _safe_str(value)
    return result


def _normalize(value: object, depth: int) -> object:
    if depth > MAX_DEPTH:
        result: object = _safe_str(value)
    elif value is None or isinstance(value, str | int):
        result = value
    elif isinstance(value, float):
        result = value if math.isfinite(value) else repr(value)
    elif isinstance(value, Mapping):
        result = {str(key): _normalize(item, depth + 1) for key, item in value.items()}
    elif isinstance(value, list | tuple):
        result = [_normalize(item, depth + 1) for item in value]
    elif isinstance(value, set | frozenset):
        result = [_normalize(item, depth + 1) for item in sorted(value, key=str)]
    else:
        result = _normalize_leaf(value, depth)
    return result


def normalize_values(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Make every value JSON-native (depth 4) before guarding, scrubbing and truncation."""
    for key in list(event_dict):
        if key not in _META_KEYS:
            event_dict[key] = _normalize(event_dict[key], 1)
    return event_dict


def _guard(key: str, value: object, is_debug: bool, depth: int) -> object:
    lowered = key.lower()
    if _is_secret_key(lowered) or (lowered in TEXT_KEYS and not is_debug):
        return OMITTED
    if isinstance(value, dict) and depth < MAX_DEPTH:
        return {str(k): _guard(str(k), v, is_debug, depth + 1) for k, v in value.items()}
    return value


def guard_sensitive(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Drop secret-named fields always and free-text fields above DEBUG (ENG §3.6)."""
    is_debug = event_dict.get("level") == "debug"
    for key in list(event_dict):
        if key not in _NEVER_GUARDED:
            event_dict[key] = _guard(key, event_dict[key], is_debug, 1)
    return event_dict


def check_event_name(strict: bool) -> Processor:
    """Return a processor enforcing component.object.action names; strict raises SchemaViolation."""

    def _check(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
        event = event_dict.get("event")
        if isinstance(event, str) and EVENT_NAME_RE.fullmatch(event):
            return event_dict
        if strict:
            msg = "invalid log event name"
            raise SchemaViolation(msg, event=str(event)[:120])
        event_dict["event_name_invalid"] = True
        return event_dict

    return _check


def _limit(value: object) -> object:
    if isinstance(value, str):
        return value if len(value) <= MAX_FIELD_CHARS else value[:MAX_FIELD_CHARS] + _TRUNCATED
    if isinstance(value, dict):
        return {key: _limit(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_limit(item) for item in value]
    return value


def limit_sizes(logger: object, method_name: str, event_dict: EventDict) -> EventDict:
    """Cut every string value longer than MAX_FIELD_CHARS characters."""
    for key in list(event_dict):
        event_dict[key] = _limit(event_dict[key])
    return event_dict


def _dump(ordered: Mapping[str, object]) -> str:
    return json.dumps(
        ordered, ensure_ascii=False, separators=(",", ":"), allow_nan=False, default=str
    )


def _largest_optional(ordered: Mapping[str, object]) -> str | None:
    victim, size = None, -1
    for key, value in ordered.items():
        if key in _KEPT_KEYS:
            continue
        length = len(_dump({"v": value}))
        if length >= size:
            victim, size = key, length
    return victim


def render_json(logger: object, method_name: str, event_dict: EventDict) -> str:
    """Render one JSON line: required keys first, at most MAX_LINE_BYTES bytes."""
    ordered: dict[str, object] = {k: event_dict[k] for k in _FIRST_KEYS if k in event_dict}
    ordered.update((k, v) for k, v in event_dict.items() if k not in ordered)
    line = _dump(ordered)
    dropped: list[str] = []
    while len(line.encode("utf-8")) > MAX_LINE_BYTES:
        victim = _largest_optional(ordered)
        if victim is None:
            ordered["event"] = str(ordered.get("event", ""))[:_EVENT_CUT]
            return _dump(ordered)
        del ordered[victim]
        dropped.append(victim)
        ordered["dropped_fields"] = sorted(dropped)
        line = _dump(ordered)
    return line


def _open_append(path: pathlib.Path) -> TextIO:
    return path.open("a", encoding="utf-8", newline="\n")


def _status_line(level: str, event: str, path: pathlib.Path, **fields: object) -> str:
    body: dict[str, object] = {
        "ts": clock.format_utc(clock.now()),
        "level": level,
        "event": event,
        "component": "core.logging",
        "path": path.as_posix(),
    }
    body.update(fields)
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))


class _SafeHandleErrorMixin:
    """Safe handleError: reports core.logging.emit_failed only, never msg/args/traceback."""

    def handleError(self, record: logging.LogRecord) -> None:  # noqa: N802 - overrides Handler
        with contextlib.suppress(Exception):
            exc_type = sys.exc_info()[0]
            body = {
                "event": "core.logging.emit_failed",
                "component": "core.logging",
                "logger": record.name[:64],
                "error_type": "Unknown" if exc_type is None else exc_type.__name__,
            }
            sys.stderr.write(json.dumps(body, ensure_ascii=False, separators=(",", ":")) + "\n")


class SafeStreamHandler(_SafeHandleErrorMixin, logging.StreamHandler[TextIO]):
    """StreamHandler to the live stderr; re-reads sys.stderr on every emit (TH00-01)."""

    def emit(self, record: logging.LogRecord) -> None:
        self.stream = sys.stderr
        super().emit(record)


class DailyJsonlHandler(_SafeHandleErrorMixin, logging.Handler):
    """Append lines to herness-<UTC date>.jsonl; format/disk failures drop the line (TH00-01)."""

    def __init__(self, log_dir: pathlib.Path) -> None:
        super().__init__()
        self._log_dir = log_dir
        self._day: str | None = None
        self._stream: TextIO | None = None
        self._failed_at: float | None = None
        self._dropped = 0

    def _ensure_stream(self) -> tuple[pathlib.Path, TextIO]:
        day = clock.utc_day(clock.now())
        path = self._log_dir / (LOG_FILE_PREFIX + day + ".jsonl")
        if day != self._day or self._stream is None:
            self._close_stream()
            self._stream = _open_append(path)
            self._day = day
        return path, self._stream

    def _close_stream(self) -> None:
        if self._stream is not None:
            with contextlib.suppress(OSError):
                self._stream.close()
        self._stream = None

    def emit(self, record: logging.LogRecord) -> None:
        """Write one formatted record; see the class docstring for failure handling."""
        try:
            line = self.format(record)
        except Exception:  # noqa: BLE001 - a formatter failure must never raise or echo raw fields
            self.handleError(record)
            return
        if self._failed_at is not None and clock.monotonic() - self._failed_at < FILE_RETRY_S:
            self._dropped += 1
            return
        day = self._day or clock.utc_day(clock.now())
        path = self._log_dir / (LOG_FILE_PREFIX + day + ".jsonl")
        try:
            path, stream = self._ensure_stream()
            if self._failed_at is not None:
                recovered = _status_line(
                    "info", "core.logging.sink_recovered", path, dropped_lines=self._dropped
                )
                stream.write(recovered + "\n")
                sys.stderr.write(recovered + "\n")
                self._failed_at = None
                self._dropped = 0
            stream.write(line + "\n")
            stream.flush()
        except OSError as exc:
            self._close_stream()
            self._failed_at = clock.monotonic()
            self._dropped += 1
            failed = _status_line(
                "error",
                "core.logging.sink_failed",
                path,
                error_type=type(exc).__name__,
                retry_in_s=FILE_RETRY_S,
            )
            sys.stderr.write(failed + "\n")

    def close(self) -> None:
        """Close the day file, then the handler."""
        self.acquire()
        try:
            self._close_stream()
        finally:
            self.release()
        super().close()
