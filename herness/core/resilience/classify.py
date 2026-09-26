"""Foreign exception → spec 00 §7 taxonomy (U08-16) and the single Retry-After parser (U08-17).

Never imports `openai`, `anthropic` or `duckdb`: their classes are looked up only when the
caller has already imported them (`sys.modules`). Messages never carry exception text; an
HTTP body is redacted and cut before it enters a message (TH08-02).
"""

from __future__ import annotations

import concurrent.futures
import email.utils
import re
import sqlite3
import sys
import types
from collections.abc import Mapping
from datetime import datetime
from typing import Final, Literal

import httpx

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import (
    AuthError,
    ConfigError,
    FatalError,
    HernessError,
    ModelUnavailable,
    QueryError,
    RateLimited,
    SourceUnavailable,
    StoreBusy,
)
from herness.core.redact import redact_text

type ErrorFamily = Literal["source", "model", "decider", "store"]

DETAIL_CHARS: Final = 500
MESSAGE_BYTES: Final = 2048
_REDACT_WINDOW: Final = 4000  # redaction runs on this prefix only: bounds its cost (U08-16)
_UNAVAILABLE: Final[Mapping[str, type[HernessError]]] = types.MappingProxyType(
    {
        "source": SourceUnavailable,
        "model": ModelUnavailable,
        "decider": ModelUnavailable,
        "store": StoreBusy,
    }
)
_UNAVAILABLE_STATUS: Final = frozenset({500, 502, 503, 504, 529})
_HTTPX_UNAVAILABLE: Final = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.ReadTimeout,
    httpx.WriteTimeout,
    httpx.PoolTimeout,
    httpx.RemoteProtocolError,
)
_DELAY_SECONDS: Final = re.compile(r"[0-9]{1,10}")
_RESET_VALUE: Final = re.compile(r"[0-9]{1,16}(\.[0-9]+)?")
_EPOCH_MS: Final = 10**12
_EPOCH_S: Final = 10**9


def _classes(module: str, *names: str) -> tuple[type[BaseException], ...]:
    """The named exception classes of an already imported SDK; empty when it is not loaded."""
    mod = sys.modules.get(module)
    found = (getattr(mod, name, None) for name in names) if mod is not None else ()
    return tuple(c for c in found if isinstance(c, type) and issubclass(c, BaseException))


def _body(response: object) -> str:
    """The response text, or "" when there is none or it cannot be read (streamed, bad codec)."""
    try:
        text = getattr(response, "text", "")
    except Exception:  # noqa: BLE001 - httpx.ResponseNotRead, decode errors: no detail
        return ""
    return text if isinstance(text, str) else ""


def _redacted(body: str) -> str:
    """Redact, then cut to 500 chars.

    Deviation (controller ruling on U08-16, which says "cut then redact"): redacting first
    means a cut at char 500 cannot split an e-mail or key into a fragment redaction misses.
    `redact_text` returns None when it fails closed; that (or any redactor error) gives "".
    """
    if not body:
        return ""
    try:
        clean = redact_text(body[:_REDACT_WINDOW])
    except Exception:  # noqa: BLE001 - fail closed: classify must return, never raise
        return ""
    return "" if clean is None else clean[:DETAIL_CHARS]


def _msg(
    family: ErrorFamily, exc: BaseException, status: int | None = None, detail: str = ""
) -> str:
    """ "<family> call failed: <ExceptionType>[ HTTP <status>][: <detail>]", at most 2 KB."""
    msg = f"{family} call failed: {type(exc).__name__}"
    msg += "" if status is None else f" HTTP {status}"
    msg += f": {detail}" if detail else ""
    return msg.encode("utf-8")[:MESSAGE_BYTES].decode("utf-8", "ignore")


def _from_status(
    exc: BaseException, family: ErrorFamily, status: int, response: object
) -> HernessError:
    """Rule 2: map an HTTP status (with its headers and body) to the taxonomy."""
    msg = _msg(family, exc, status, _redacted(_body(response)))
    if status == 429:  # noqa: PLR2004 - HTTP status
        headers = getattr(response, "headers", None)
        now = clock.now()
        wait = parse_retry_after(headers, now) if isinstance(headers, Mapping) else None
        return RateLimited(msg, retry_after=wait)
    if status in {401, 403}:
        return AuthError(msg)
    if status in _UNAVAILABLE_STATUS:
        return _UNAVAILABLE[family](msg)
    if msg.endswith(f"HTTP {status}"):  # no body detail: name the reason instead
        msg += f": unexpected HTTP {status}"
    return ConfigError(msg)


def _from_sdk(exc: BaseException, family: ErrorFamily) -> HernessError | None:
    """Rule 4: openai / anthropic errors, only when the SDK is already in `sys.modules`."""
    if isinstance(exc, _classes("anthropic", "OverloadedError")):
        return ModelUnavailable(_msg(family, exc))
    for sdk in ("openai", "anthropic"):
        if isinstance(exc, _classes(sdk, "APIConnectionError", "APITimeoutError")):
            return ModelUnavailable(_msg(family, exc))
        if isinstance(exc, _classes(sdk, "APIStatusError")):
            status = getattr(exc, "status_code", None)
            if isinstance(status, int):
                return _from_status(exc, family, status, getattr(exc, "response", None))
    return None


def _from_store(exc: BaseException, family: ErrorFamily) -> HernessError | None:
    """Rules 5 and 6: SQLite and DuckDB lock texts (the only message matching allowed)."""
    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        if "locked" in text or "busy" in text:
            return StoreBusy(_msg(family, exc))
        return FatalError(_msg(family, exc, detail="sqlite error"))
    if isinstance(exc, _classes("duckdb", "InterruptException")):
        return QueryError(_msg(family, exc, detail="query interrupted after timeout"), timeout=True)
    if isinstance(exc, _classes("duckdb", "IOException")) and "lock" in str(exc).lower():
        return StoreBusy(_msg(family, exc))
    return None


def classify(exc: BaseException, *, family: ErrorFamily) -> HernessError:
    """Map a foreign exception to the taxonomy; the first matching rule of U08-16 wins."""
    if isinstance(exc, HernessError):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        return _from_status(exc, family, exc.response.status_code, exc.response)
    mapped = _from_sdk(exc, family) or _from_store(exc, family)
    if mapped is not None:
        return mapped
    if isinstance(exc, (*_HTTPX_UNAVAILABLE, TimeoutError, concurrent.futures.TimeoutError)):
        return _UNAVAILABLE[family](_msg(family, exc))
    return FatalError(_msg(family, exc, detail=f"unclassified {type(exc).__name__}"))


def _header(headers: Mapping[str, str], name: str) -> str | None:
    """Case-insensitive header lookup that works for plain dicts and `httpx.Headers`."""
    for key, value in headers.items():
        if str(key).lower() == name:
            return str(value).strip()
    return None


def _retry_after_delay(value: str, now: datetime) -> float | None:
    """RFC 9110 delay-seconds or HTTP-date; None when the value is neither."""
    if _DELAY_SECONDS.fullmatch(value):
        return float(value)
    try:
        date = email.utils.parsedate_to_datetime(value)
        return None if date.tzinfo is None else (date - now).total_seconds()
    except (TypeError, ValueError, IndexError, OverflowError):
        return None


def _reset_delay(value: str, now: datetime) -> float | None:
    """`X-RateLimit-Reset` as epoch milliseconds, epoch seconds or a delay in seconds."""
    if not _RESET_VALUE.fullmatch(value):
        return None
    v = float(value)
    if v >= _EPOCH_MS:
        return v / 1000 - now.timestamp()
    return v - now.timestamp() if v >= _EPOCH_S else v


def parse_retry_after(
    headers: Mapping[str, str], now: datetime, *, max_s: float | None = None
) -> float | None:
    """Seconds to wait from `Retry-After` (else `X-RateLimit-Reset`), in [0, max_s]; never raises.

    `max_s` None reads `R.retry.retry_after_max_s`, only once a header is valid.
    """
    raw = _header(headers, "retry-after")
    delay = None if raw is None else _retry_after_delay(raw, now)
    if delay is None:
        reset = _header(headers, "x-ratelimit-reset")
        delay = None if reset is None else _reset_delay(reset, now)
    if delay is None:
        return None
    if max_s is None:
        max_s = get_config().resilience.resilience.retry.retry_after_max_s
    return min(max(delay, 0.0), max_s)


class _CallableModule(types.ModuleType):
    """This module and U08-16's function share the name `herness.core.resilience.classify`;
    a callable module keeps `resilience.classify(exc, family=...)` working whichever one the
    package attribute holds (the import system sets it to the module)."""

    def __call__(self, exc: BaseException, *, family: ErrorFamily) -> HernessError:
        return classify(exc, family=family)


sys.modules[__name__].__class__ = _CallableModule
