"""Cited numbers, recorded evidence and verification results (impl 05 U05-10 … U05-12).

Design 05 §3.5, §4.5 … §4.7. Markers only: every number in prose cites a `NumberRef`
whose `query_id` the Verifier re-runs (TH05-11, LLM09).
"""

import math
import re
from datetime import UTC, datetime
from typing import Annotated, Literal, Self

from pydantic import (
    AfterValidator,
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from herness.core.errors import SchemaViolation
from herness.core.ids import query_id as compute_query_id

_QUERY_ID_PATTERN = r"^q_[0-9a-f]{16}$"
_USD_RE = re.compile(r"-?[0-9]+(\.[0-9]+)?", re.ASCII)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "datetime must be timezone-aware UTC"
        raise ValueError(msg)
    return value.astimezone(UTC)


def _no_bool(value: object) -> object:
    if isinstance(value, bool):
        msg = "a cited value cannot be a boolean"
        raise ValueError(msg)  # noqa: TRY004 - pydantic needs ValueError
    return value


type _UtcDatetime = Annotated[datetime, AfterValidator(_utc)]
type _Scalar = str | int | Annotated[float, Field(allow_inf_nan=False)] | bool | None
type _RowKey = Annotated[dict[str, _Scalar], Field(max_length=16)]
type _Claimed = Annotated[float | int | str, BeforeValidator(_no_bool)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NumberRef(_Frozen):
    """A cited number (spec 00 §12.1); not strict because JSON numbers arrive as int or float."""

    id: str = Field(pattern=r"^n[0-9]+$")
    value: _Claimed
    unit: Literal[
        "count", "usd", "pct", "ratio", "hours", "minutes", "seconds", "days", "score", "rank",
        "other",
    ]  # fmt: skip
    query_id: str = Field(pattern=_QUERY_ID_PATTERN)
    column: str = Field(min_length=1, max_length=128)
    row_key: _RowKey | None
    format: (
        Literal["usd", "usd_compact", "int", "pct1", "ratio2", "hours1", "minutes0", "prob2"] | None
    ) = None

    @model_validator(mode="after")
    def _value_matches_unit(self) -> Self:
        if self.unit == "usd":
            if not isinstance(self.value, str) or _USD_RE.fullmatch(self.value) is None:
                msg = "a usd value must be a decimal string"
                raise ValueError(msg)
        elif isinstance(self.value, str) or (
            isinstance(self.value, float) and not math.isfinite(self.value)
        ):
            msg = f"a {self.unit} value must be a finite number"
            raise ValueError(msg)
        return self


class Evidence(_Frozen):
    """One recorded query, 1:1 with ops `evidence` (design §4.6)."""

    query_id: str = Field(pattern=_QUERY_ID_PATTERN)
    run_id: str | None
    build_id: str
    sql: str = Field(max_length=20_000)
    params: dict[str, JsonValue]
    result_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    row_count: int = Field(ge=0, strict=True)
    result_sample: list[dict[str, JsonValue]] = Field(max_length=50)
    executed_at: _UtcDatetime
    duration_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _query_id_matches(self) -> Self:
        try:
            expected = compute_query_id(self.sql, self.params, self.build_id)
        except SchemaViolation as exc:
            msg = "evidence sql, params or build_id cannot form a query_id"
            raise ValueError(msg) from exc
        if expected != self.query_id:
            msg = "evidence query_id does not match its sql, params and build_id"
            raise ValueError(msg)
        return self


class NumberCheck(_Frozen):
    """The Verifier's comparison of one cited number (design §4.7)."""

    number_id: str
    query_id: str
    column: str
    row_key: _RowKey | None
    claimed: _Claimed
    actual: _Claimed | None
    result: Literal[
        "match", "mismatch", "missing_query", "wrong_build", "query_failed", "missing_column",
        "row_not_found", "row_ambiguous",
    ]  # fmt: skip


class UncitedSpan(_Frozen):
    """A numeral in prose that no marker cites."""

    text: str
    start: int = Field(ge=0, strict=True)
    end: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        if self.end < self.start:
            msg = "uncited span end is before its start"
            raise ValueError(msg)
        return self


class ItemResult(_Frozen):
    """Verification outcome of one item; `claim_support` stays None (R-37)."""

    where: str
    passed: bool
    checks: list[NumberCheck]
    uncited: list[UncitedSpan]
    unknown_markers: list[str]
    bad_refs: list[str]
    unverified_findings: list[str]
    claim_support: Literal["yes", "partial", "no"] | None = None

    @model_validator(mode="after")
    def _passed_consistent(self) -> Self:
        clean = not (self.uncited or self.unknown_markers or self.bad_refs)
        expected = (
            clean
            and not self.unverified_findings
            and all(check.result == "match" for check in self.checks)
        )
        if self.passed != expected:
            msg = f"item passed={self.passed} contradicts its checks and lists"
            raise ValueError(msg)
        return self


class VerificationResult(_Frozen):
    """Verifier output for one call (design §4.7)."""

    build_id: str
    passed: bool
    items: list[ItemResult]
    n_numbers: int = Field(ge=0, strict=True)
    n_failed: int = Field(ge=0, strict=True)
    verified_at: _UtcDatetime
    duration_ms: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def _totals_consistent(self) -> Self:
        checks = [check for item in self.items for check in item.checks]
        failed = sum(1 for check in checks if check.result != "match")
        if self.passed != all(item.passed for item in self.items):
            msg = f"result passed={self.passed} contradicts its items"
            raise ValueError(msg)
        if (self.n_numbers, self.n_failed) != (len(checks), failed):
            msg = f"counts {self.n_numbers}/{self.n_failed} != {len(checks)}/{failed}"
            raise ValueError(msg)
        return self


class VerifiableItem(_Frozen):
    """Verifier input: prose with `[[<id>]]` markers and the numbers it cites (design §3.5)."""

    where: str = Field(max_length=200)
    text: str = Field(max_length=20_000)
    numbers: list[NumberRef] = Field(max_length=200)
    finding_ids: list[str] = Field(default=[], max_length=200)
    refs: dict[str, str] = Field(default={}, max_length=50)
