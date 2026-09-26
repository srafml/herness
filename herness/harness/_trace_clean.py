"""Trace field and payload cleaning for ``herness.harness.tracing`` (U05-69; TH05-14).

Both walks turn values into JSON-safe data (Decimal and other objects -> str, datetime -> UTC
ISO Z, pydantic models -> dumped), drop every ``opaque`` key at any depth and clean every
string, dict keys included. Field strings pass the secret scrubber; payload strings pass
``redact_text`` and the scrubber on the whole string and only then the per-message cut.
"""

from __future__ import annotations

import datetime
import enum
import math
from collections.abc import Callable, Mapping
from typing import Any, Final

from pydantic import BaseModel

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.redact import redact_text
from herness.core.secrets import known_values, scrub_secrets

__all__ = ["clean_fields", "clean_payload"]

type _Text = Callable[[str], str | None]
_MAX_DEPTH: Final = 64
# herness.core.secrets scrubs at most the first 64 KiB of a string (U10-32).
_SCRUB_LIMIT: Final = 64 * 1024
# Minimum tail dropped past that limit: also covers pattern-found credentials (ghp_, xox...).
_MIN_CUT_TAIL: Final = 512


def _scrub(text: str) -> str | None:
    """Mask known secret values and credential spans; ``None`` when the scrubber fails closed."""
    out = scrub_secrets(None, "trace", {"t": text}).get("t")
    if not isinstance(out, str):
        return None
    if len(text) > _SCRUB_LIMIT:  # the scrubber saw a prefix: a cut secret can only be the tail
        tail = max(_MIN_CUT_TAIL, max(map(len, known_values()), default=0))
        return out[: max(0, len(out) - tail)]
    return out


def _utc(value: datetime.datetime) -> str:
    """UTC ISO Z text; a naive datetime is taken as UTC."""
    aware = value if value.utcoffset() is not None else value.replace(tzinfo=datetime.UTC)
    return clock.format_utc(aware)


def _leaf(value: object, text: _Text) -> object:
    if isinstance(value, enum.Enum):
        value = value.value
    if value is None or isinstance(value, int):  # bool is an int
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, datetime.datetime):
        return _utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    return text(value if isinstance(value, str) else str(value))  # Decimal, Path, bytes, ...


def _mapping(value: Mapping[Any, object], text: _Text, depth: int) -> dict[str, object]:
    out: dict[str, object] = {}
    for key, item in value.items():
        name = key if isinstance(key, str) else str(key)
        if name == "opaque":
            continue
        clean = text(name)
        if clean is not None:  # a key that cannot be cleaned is dropped with its value
            out[clean] = _convert(item, text, depth + 1)
    return out


def _convert(value: object, text: _Text, depth: int) -> object:
    if depth > _MAX_DEPTH:
        msg = "trace value nested too deeply"
        raise ConfigError(msg)
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, Mapping):
        return _mapping(value, text, depth)
    if isinstance(value, list | tuple | set | frozenset):
        return [_convert(item, text, depth + 1) for item in value]
    return _leaf(value, text)


def clean_fields(fields: Mapping[str, object]) -> dict[str, object]:
    """JSON-safe, scrubbed event fields without ``opaque``; ConfigError when not serializable."""
    try:
        return _mapping(fields, _scrub, 0)
    except Exception as exc:
        msg = "trace fields cannot be serialized"
        raise ConfigError(msg, error_type=type(exc).__name__) from exc


def clean_payload(payload: object, max_chars: int) -> object:
    """JSON-safe payload: each string redacted and scrubbed whole, then cut to ``max_chars``.

    The 64 KiB scrub limit thereby caps strings near 64 KiB even if ``max_chars`` is larger.
    """

    def text(value: str) -> str | None:
        redacted = redact_text(value)
        scrubbed = None if redacted is None else _scrub(redacted)
        return None if scrubbed is None else scrubbed[:max_chars]

    return _convert(payload, text, 0)
