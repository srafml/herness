"""Section model of `config/app.yaml` (design 09 §7, §9.1; U09-02; R-03).

`AppConfig` is the root spec 10 mounts at `cfg.app`. This module imports only the
standard library, pydantic and `herness.core.types` (R-03, contract
`settings modules are leaves`); it never imports `duckdb`, `jinja2` or `streamlit`, so
spec 10's config loader can import it cheaply. Section registration (mounting this model
at `cfg.app`) is T10-03's job, not this module's.
"""

from __future__ import annotations

import re
from functools import cached_property
from typing import Annotated, Final, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "AppConfig",
    "AppSection",
    "CacheTtl",
    "ChatSection",
    "CliSection",
    "ReportsSection",
]

type ReportFormat = Literal["html", "md", "pdf"]
_DEFAULT_FORMATS: Final[tuple[ReportFormat, ...]] = ("html", "md")
_DEFAULT_NUMERAL_PATTERNS: Final = (
    r"\b(19|20)\d{2}\b",
    r"\d{4}-\d{2}-\d{2}",
    r"Q[1-4] \d{4}",
    r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
)
_MAX_PATTERN_CHARS: Final = 200
_MAX_WINDOW_DAYS: Final = 3650


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def _compiles(value: str) -> str:
    try:
        re.compile(value)
    except re.error as exc:
        msg = "not a valid regular expression"
        raise ValueError(msg) from exc
    return value


_NumeralPattern = Annotated[str, Field(max_length=_MAX_PATTERN_CHARS), AfterValidator(_compiles)]


class _Model(BaseModel):
    """Base of every model here: closed and immutable (U09-02)."""

    model_config = ConfigDict(extra="forbid", frozen=True)


class CacheTtl(_Model):
    """`app.cache_ttl_s`: dashboard cache lifetimes."""

    warehouse: int = Field(default=3600, ge=1, le=86400)
    ops: int = Field(default=10, ge=1, le=3600)


class AppSection(_Model):
    """`app`: dashboard cache and paging (U09-02)."""

    cache_ttl_s: CacheTtl = Field(default_factory=CacheTtl)
    current_recheck_s: int = Field(default=60, ge=5, le=3600)
    page_row_limit: int = Field(default=5000, ge=100, le=50000)


class ChatSection(_Model):
    """`chat`: question and polling limits (U09-02)."""

    max_question_chars: int = Field(default=4000, ge=100, le=20000)
    poll_queued_s: int = Field(default=15, ge=5, le=300)


class ReportsSection(_Model):
    """`reports`: render options shared with the Verifier (U09-02)."""

    formats: Annotated[list[ReportFormat], Field(min_length=1)] = Field(
        default_factory=lambda: list(_DEFAULT_FORMATS)
    )
    top_n: int = Field(default=10, ge=1, le=100)
    strict_numbers: bool = True
    evidence_sample_rows: int = Field(default=20, ge=0, le=50)
    allowed_numeral_patterns: Annotated[
        list[_NumeralPattern], Field(min_length=1, max_length=50)
    ] = Field(default_factory=lambda: list(_DEFAULT_NUMERAL_PATTERNS))
    prior_outcomes_window_days: tuple[int, int] = (60, 120)

    @field_validator("formats")
    @classmethod
    def _formats_unique(cls, value: list[ReportFormat]) -> list[ReportFormat]:
        _require(len(set(value)) == len(value), "formats must not repeat a value")
        return value

    @field_validator("prior_outcomes_window_days")
    @classmethod
    def _window_order(cls, value: tuple[int, int]) -> tuple[int, int]:
        first, second = value
        ok = 0 <= first < second <= _MAX_WINDOW_DAYS
        _require(ok, "prior_outcomes_window_days must satisfy 0 <= first < second <= 3650")
        return value

    @cached_property
    def compiled_numeral_patterns(self) -> tuple[re.Pattern[str], ...]:
        """The patterns compiled once, in file order (U09-02 postconditions)."""
        return tuple(re.compile(p) for p in self.allowed_numeral_patterns)


class CliSection(_Model):
    """`cli`: polling cadence for long-running commands (U09-02)."""

    poll_interval_s: float = Field(default=2.0, ge=0.5, le=60)


class AppConfig(_Model):
    """Root of `config/app.yaml`, mounted by spec 10 at `cfg.app` (U09-02)."""

    version: Literal[1] = 1
    app: AppSection = Field(default_factory=AppSection)
    chat: ChatSection = Field(default_factory=ChatSection)
    reports: ReportsSection = Field(default_factory=ReportsSection)
    cli: CliSection = Field(default_factory=CliSection)
