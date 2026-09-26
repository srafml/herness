"""Error taxonomy of design 00 §7, plus NotFound, hint and details (R-19).

Every error Herness raises is one of these classes, so the resilience layer (impl 08)
decides by class, never by string matching. Spec-local subclasses are declared by their
owners, each in its own section at the end of this file (R-19).
"""

from __future__ import annotations

import datetime
import math
import re
import types
from collections.abc import Iterable, Mapping, Sequence
from typing import ClassVar, Final, Literal

MAX_MESSAGE_CHARS: Final = 1000
MAX_CONTEXT_STR_CHARS: Final = 200
MAX_HINT_CHARS: Final = 500
MAX_DETAILS: Final = 50
MAX_DETAIL_KEY_CHARS: Final = 64
MAX_DETAIL_VALUE_CHARS: Final = 2000
_MAX_CATEGORY_CHARS: Final = 64
_DETAIL_KEY_RE: Final = re.compile(r"[A-Za-z0-9_.\-]{1,64}")
_ELLIPSIS: Final = "…"

type Scalar = str | int | float | bool | None
type ErrorKind = Literal["retryable", "recoverable", "fatal", "unknown"]
type _Kw = str | Mapping[str, str] | None  # EgressBlocked hint or details (impl 10 U10-108)


def _bound(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + _ELLIPSIS


def _type_tag(value: object) -> str:
    return "<" + type(value).__name__ + ">"


def _scalar(value: object, limit: int) -> Scalar:
    if value is None or isinstance(value, bool | int | float):
        return value
    if isinstance(value, str):
        return _bound(value, limit)
    return _type_tag(value)


def _bound_context(items: Iterable[tuple[str, object]]) -> dict[str, Scalar]:
    return {key: _scalar(value, MAX_CONTEXT_STR_CHARS) for key, value in items}


def _bound_hint(hint: object) -> str | None:
    if hint is None:
        return None
    return _bound(hint, MAX_HINT_CHARS) if isinstance(hint, str) else _type_tag(hint)


def _bound_details(items: Iterable[tuple[object, object]] | None) -> dict[str, str]:
    det: dict[str, str] = {}
    if items is None:
        return det
    for index, (key, value) in enumerate(items):
        if index >= MAX_DETAILS:
            break
        valid = isinstance(key, str) and _DETAIL_KEY_RE.fullmatch(key) is not None
        name = key if valid and isinstance(key, str) else f"key_{index}"
        det[name] = (
            _bound(value, MAX_DETAIL_VALUE_CHARS) if isinstance(value, str) else _type_tag(value)
        )
    return det


class HernessError(Exception):
    """Root of every error Herness raises.

    Carries a bounded message, a scalar-only identifier context, an optional operator
    hint and optional string details. The constructor raises nothing.
    """

    _extra_attrs: ClassVar[tuple[str, ...]] = ()
    message: str
    hint: str | None
    _context: dict[str, Scalar]
    _details: dict[str, str]

    def __init__(
        self,
        message: str,
        /,
        *,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        bounded = _bound(message, MAX_MESSAGE_CHARS)
        super().__init__(bounded)
        self.message = bounded
        self._context = _bound_context(context.items())
        self.hint = _bound_hint(hint)
        self._details = _bound_details(None if details is None else details.items())

    @property
    def context(self) -> Mapping[str, Scalar]:
        """Read-only identifier context."""
        return types.MappingProxyType(self._context)

    @property
    def details(self) -> Mapping[str, str]:
        """Read-only structured details (R-19)."""
        return types.MappingProxyType(self._details)

    def __reduce__(self) -> tuple[object, ...]:
        extra = {name: getattr(self, name) for name in type(self)._extra_attrs}
        return (
            _rebuild,
            (type(self), self.message, dict(self._context), self.hint, dict(self._details), extra),
        )


def _rebuild(
    cls: type[HernessError],
    message: str,
    context: dict[str, Scalar],
    hint: str | None,
    details: dict[str, str],
    extra: dict[str, object],
) -> HernessError:
    obj = cls.__new__(cls)
    Exception.__init__(obj, message)
    obj.message = message
    obj._context = context
    obj.hint = hint
    obj._details = details
    for name, value in extra.items():
        setattr(obj, name, value)
    return obj


class RetryableError(HernessError):
    """Retried with backoff by the resilience layer (impl 08)."""


class RecoverableError(HernessError):
    """The caller repairs and retries differently."""


class FatalError(HernessError):
    """No retry; the job fails and the task goes to the dead letter list."""


class SourceUnavailable(RetryableError):
    """A source system cannot be reached or returned a server error."""


class ModelUnavailable(RetryableError):
    """A model endpoint cannot be reached or is overloaded."""


class StoreBusy(RetryableError):
    """SQLite or DuckDB lock contention outlasted its busy timeout."""


class RateLimited(RetryableError):
    """A rate limit was hit; carries the server-requested wait (seconds or None)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("retry_after",)
    retry_after: float | None

    def __init__(
        self,
        message: str,
        /,
        *,
        retry_after: float | None = None,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        if retry_after is None or not math.isfinite(retry_after):
            self.retry_after = None
        elif retry_after < 0:
            self.retry_after = 0.0
        else:
            self.retry_after = float(retry_after)


class CircuitOpen(RetryableError):
    """A circuit breaker is open; the caller must not call until retry_at (UTC)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("key", "retry_at")
    key: str
    retry_at: datetime.datetime

    def __init__(
        self,
        message: str,
        /,
        *,
        key: str,
        retry_at: datetime.datetime,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.key = key
        if retry_at.tzinfo is None or retry_at.utcoffset() is None:
            self.retry_at = retry_at.replace(tzinfo=datetime.UTC)
        else:
            self.retry_at = retry_at.astimezone(datetime.UTC)


class OutputValidationError(RecoverableError):
    """Model output failed schema validation; repair prompt (max 2), then fallback model."""


class ToolInputError(RecoverableError):
    """Tool arguments are invalid; returned as an error tool result."""


class QueryError(RecoverableError):
    """SQL failed or was rejected; returned as an error tool result with a hint."""


class ModelRefused(RecoverableError):
    """The model refused to answer; the fallback chain moves to its next entry."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("category",)
    category: str | None

    def __init__(
        self,
        message: str,
        /,
        *,
        category: str | None = None,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.category = None if category is None else category[:_MAX_CATEGORY_CHARS]


class PolicyViolation(RecoverableError):
    """Memory write policy or injection scan refused a write; pending or rejected."""


class ReportContractError(RecoverableError):
    """A draft fails the rendering contract."""


class NotFound(RecoverableError):
    """A requested object does not exist; the caller reports it or picks another (R-19)."""


class ConfigError(FatalError):
    """Configuration is invalid or incomplete; ``issues`` holds impl 10 ``ConfigIssue``s (R-19)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("issues",)

    def __init__(
        self,
        message: str,
        /,
        *,
        issues: Sequence[object] = (),
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.issues: tuple[object, ...] = tuple(issues)[:1000]


class AuthError(FatalError):
    """Authentication with a source or model endpoint failed."""


class SchemaViolation(FatalError):
    """Data does not match its declared contract."""


class BudgetExceeded(FatalError):
    """A token, cost, tool-call or wall-clock budget is exhausted."""


class PermissionDenied(FatalError):
    """The caller's role is not allowed to perform the action."""


class EgressBlocked(FatalError):
    """The egress guard refused an off-network call; masked ``egress_id``, ``reason`` (R-19)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("egress_id", "reason")

    def __init__(
        self, message: str, *, egress_id: str | None = None, reason: str | None = None, **kw: _Kw
    ) -> None:
        super().__init__(message, **kw)  # type: ignore[arg-type]  # kw holds only hint and details
        self.egress_id = egress_id if isinstance(egress_id, str) else None  # egr_<ulid> or None
        ok = reason is None or re.fullmatch(r"[a-z_]{1,40}", str(reason)) is not None
        self.reason = reason if ok else "invalid"  # a malformed code is never stored or logged


# --- 08 (resilience and jobs) ---


class JobStateError(FatalError):
    """A job or task is not in the state the operation needs (R-19).

    Unknown job_id, retry of a non-failed job, checkpoint or completion of a task that
    is not running, or an inline run of a job that cannot be claimed.
    """

    _extra_attrs: ClassVar[tuple[str, ...]] = ("job_id", "task_id", "run_id")
    job_id: str | None
    task_id: str | None
    run_id: str | None

    def __init__(
        self,
        message: str,
        /,
        *,
        job_id: str | None = None,
        task_id: str | None = None,
        run_id: str | None = None,
        hint: str | None = None,
        details: Mapping[str, str] | None = None,
        **context: Scalar,
    ) -> None:
        super().__init__(message, hint=hint, details=details, **context)
        self.job_id = job_id
        self.task_id = task_id
        self.run_id = run_id


def error_kind(exc: BaseException) -> ErrorKind:
    """Classify any exception into its taxonomy category. Raises nothing."""
    if isinstance(exc, RetryableError):
        return "retryable"
    if isinstance(exc, RecoverableError):
        return "recoverable"
    if isinstance(exc, FatalError):
        return "fatal"
    return "unknown"


def _attr_value(value: object) -> Scalar:
    if isinstance(value, datetime.datetime):
        return value.isoformat()
    return _scalar(value, MAX_DETAIL_VALUE_CHARS)


def _herness_fields(out: dict[str, Scalar], exc: HernessError) -> None:
    out["error_message"] = exc.message
    out["error_hint"] = exc.hint
    for detail_key, detail_value in exc.details.items():
        out["detail_" + detail_key] = detail_value
    for name in type(exc)._extra_attrs:
        out[name] = _attr_value(getattr(exc, name))
    for ctx_key, ctx_value in exc.context.items():
        out["ctx_" + ctx_key if ctx_key in out else ctx_key] = ctx_value


def to_log_fields(exc: BaseException) -> dict[str, Scalar]:
    """Turn an exception into safe, flat, JSON-serialisable fields. Raises nothing.

    Messages of non-Herness exceptions and of causes are never copied (TH00-06).
    """
    out: dict[str, Scalar] = {"error_type": type(exc).__name__, "error_kind": error_kind(exc)}
    if isinstance(exc, HernessError):
        _herness_fields(out, exc)
    else:
        out["error_message"] = ""
    cause = exc.__cause__ or exc.__context__
    out["cause_type"] = None if cause is None else type(cause).__name__
    return out
