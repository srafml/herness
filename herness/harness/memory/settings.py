"""Section models for config/memory.yaml and config/injection_patterns.txt (U07-18, U07-19).

Imports only the standard library, pydantic, herness.core.types and herness.core.errors (R-03),
so the spec 10 config loader can import it without the rest of the harness. Defaults equal the
shipped memory.yaml (design 07 §7, with outcome.window_weeks = 10 per R-34).
"""

import math
import re
from collections.abc import Callable
from typing import Annotated, Final, Literal, Self, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from herness.core.errors import ConfigError
from herness.core.types import Kind

MAX_PATTERNS: Final = 500
MAX_PATTERN_CHARS: Final = 500
_WEIGHT_TOLERANCE: Final = 1e-9
_KINDS: Final = frozenset(get_args(Kind))
# fmt: off
_HALF_LIFE: Final[dict[Kind, int]] = {
    "run_summary": 90, "outcome_summary": 365, "decision_note": 180, "glossary": 3650,
    "business_rule": 3650, "mapping": 3650, "insight": 120, "user_correction": 180,
    "sql_template": 365, "qa_pair": 365, "analysis_recipe": 365,
}
_EXPIRY: Final[dict[Kind, int]] = {
    "run_summary": 400, "outcome_summary": 730, "decision_note": 730, "insight": 180,
    "user_correction": 365, "sql_template": 365, "qa_pair": 365, "analysis_recipe": 365,
}
# fmt: on

_WeekKey = Literal["measure_after_weeks", "second_measure_weeks", "window_weeks", "settle_weeks"]
_Open = Annotated[float, Field(gt=0, lt=1)]  # (0, 1)
_Unit = Annotated[float, Field(gt=0, le=1)]  # (0, 1]
_PosInt = Annotated[int, Field(gt=0)]


def _as_tuple(value: object) -> object:
    if isinstance(value, dict):
        return {key: _as_tuple(item) for key, item in value.items()}
    return tuple(_as_tuple(item) for item in value) if isinstance(value, list) else value


def _check_pattern(pattern: str) -> str:
    """One injection pattern: non-empty, at most 500 chars, compiles with re.IGNORECASE."""
    if not pattern.strip():
        msg = "pattern is empty"
        raise ValueError(msg)
    if len(pattern) > MAX_PATTERN_CHARS:
        msg = f"pattern longer than {MAX_PATTERN_CHARS} characters"
        raise ValueError(msg)
    try:
        re.compile(pattern, re.IGNORECASE)
    except re.error as exc:
        msg = f"pattern does not compile: {exc.msg}"
        raise ValueError(msg) from None
    return pattern


def _all_kinds(value: dict[Kind, int]) -> dict[Kind, int]:
    missing = sorted(_KINDS - set(value))
    if missing:
        msg = f"every kind needs a half-life; missing: {', '.join(missing)}"
        raise ValueError(msg)
    return value


def _weeks_ok(value: dict[str, int]) -> bool:
    return all(n >= 1 for key, n in value.items() if key != "settle_weeks")


def _rule[T](test: Callable[[T], bool], msg: str) -> AfterValidator:
    """An after-validator raising ValueError(msg) when `test(value)` is false."""

    def check(value: T) -> T:
        _require(test(value), msg)
        return value

    return AfterValidator(check)


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def parse_injection_patterns(text: str) -> tuple[str, ...]:
    """Patterns from injection_patterns.txt; `ConfigError` names the first bad line (1-based).

    Blank lines and lines starting with `#` (after leading whitespace) are skipped; other lines
    are kept verbatim except for the line ending, so trailing spaces stay significant.
    """
    patterns: list[str] = []
    for number, raw in enumerate(text.split("\n"), start=1):
        line = raw.removesuffix("\r")
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        try:
            patterns.append(_check_pattern(line))
        except ValueError as exc:
            msg = f"injection_patterns line {number}: {exc}"
            raise ConfigError(msg, key="injection_patterns", line=number) from None
    if len(patterns) > MAX_PATTERNS:
        msg = f"injection_patterns has more than {MAX_PATTERNS} patterns"
        raise ConfigError(msg, key="injection_patterns")
    return tuple(patterns)


_MetricName = Annotated[str, Field(min_length=1, max_length=128)]
_Weeks = Annotated[
    dict[_WeekKey, Annotated[int, Field(ge=0)]],
    _rule(_weeks_ok, "week counts must be >= 1 (settle_weeks >= 0)"),
]


def _per_metric_default() -> dict[str, dict[_WeekKey, int]]:
    return {"change_failure_rate": {"measure_after_weeks": 8}}


class _Section(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True, allow_inf_nan=False)

    @model_validator(mode="before")
    @classmethod
    def _lists_to_tuples(cls, data: object) -> object:
        return _as_tuple(data)


class ToolGroupKeep(_Section):
    local: int = Field(3, ge=1, le=20)
    claude: int = Field(8, ge=1, le=20)


class CompactionConfig(_Section):
    """Context compaction thresholds (U07-18)."""

    soft_ratio: _Open = 0.70
    hard_ratio: _Open = 0.85
    target_ratio: _Open = 0.45
    keep_last_tool_groups: ToolGroupKeep = Field(default_factory=ToolGroupKeep)
    summary_max_tokens: int = Field(800, ge=100, le=4000)
    ledger_sample_max_cells: int = Field(60, ge=0, le=500)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _require(
            self.target_ratio < self.soft_ratio < self.hard_ratio,
            "ratios need 0 < target_ratio < soft_ratio < hard_ratio < 1",
        )
        return self


class RecallWeights(_Section):
    """Hybrid recall score weights; each >= 0, summing to 1 within 1e-9."""

    sim: float = Field(0.55, ge=0)
    kw: float = Field(0.25, ge=0)
    ent: float = Field(0.20, ge=0)

    @model_validator(mode="after")
    def _sum_one(self) -> Self:
        total = math.fsum((self.sim, self.kw, self.ent))
        _require(abs(total - 1.0) <= _WEIGHT_TOLERANCE, "weights must sum to 1.0")
        return self


class RecallCandidates(_Section):
    vector: int = Field(50, ge=1, le=200)
    keyword: int = Field(50, ge=1, le=200)
    entity: int = Field(50, ge=1, le=200)


class RecallConfig(_Section):
    """Recall scoring and candidate limits."""

    weights: RecallWeights = Field(default_factory=RecallWeights)
    conf_floor: _Unit = 0.6
    rec_floor: _Unit = 0.7
    min_score: float = Field(0.30, ge=0, lt=1)
    candidates: RecallCandidates = Field(default_factory=RecallCandidates)
    mmr_lambda: float = Field(0.8, ge=0, le=1)
    half_life_days: Annotated[dict[Kind, _PosInt], AfterValidator(_all_kinds)] = Field(
        default_factory=lambda: dict(_HALF_LIFE)
    )


class RateLimits(_Section):
    per_run: int = Field(50, ge=1)
    per_chat_session: int = Field(10, ge=1)
    corrections_per_user_day: int = Field(3, ge=1)


class DedupeConfig(_Section):
    """Near-duplicate thresholds; conflict_cosine < merge_cosine <= 1."""

    merge_cosine: _Unit = 0.92
    conflict_cosine: _Unit = 0.80

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _require(
            self.conflict_cosine < self.merge_cosine, "needs conflict_cosine < merge_cosine <= 1"
        )
        return self


class WriteConfig(_Section):
    """Write-policy limits (U07-18)."""

    expiry_days: dict[Kind, _PosInt] = Field(default_factory=lambda: dict(_EXPIRY))
    max_content_chars: int = Field(2000, ge=100, le=8000)
    max_sql_chars: int = Field(8000, ge=500, le=20_000)
    max_data_bytes: int = Field(16384, ge=1024, le=65_536)
    max_numbers: int = Field(20, ge=1, le=50)
    rate_limits: RateLimits = Field(default_factory=RateLimits)
    dedupe: DedupeConfig = Field(default_factory=DedupeConfig)


class EpisodicConfig(_Section):
    prior_runs: int = Field(2, ge=0, le=10)
    prior_accepted_lookback_days: int = Field(400, ge=1, le=3650)


class OutcomeConfig(_Section):
    """Outcome measurement; defaults are a 2-week settle plus a 10-week window (R-34)."""

    measure_after_weeks: int = Field(12, ge=1)
    second_measure_weeks: int = Field(26, ge=1)
    window_weeks: int = Field(10, ge=1)
    settle_weeks: int = Field(2, ge=0)
    min_peers: int = Field(3, ge=1)
    min_weeks: int = Field(6, ge=1)
    min_coverage: _Unit = 0.8
    t_crit: float = Field(2.0, gt=0)
    min_rel: _Open = 0.05
    per_metric: dict[_MetricName, _Weeks] = Field(default_factory=_per_metric_default)

    @model_validator(mode="after")
    def _second_after_first(self) -> Self:
        for metric, override in {"": {}, **self.per_metric}.items():
            first = override.get("measure_after_weeks", self.measure_after_weeks)
            if not override.get("second_measure_weeks", self.second_measure_weeks) > first:
                where = f" (per_metric {metric})" if metric else ""
                msg = f"second_measure_weeks must exceed measure_after_weeks{where}"
                raise ValueError(msg)
        return self


class FeedbackConfig(_Section):
    """Confidence feedback from similar outcomes."""

    sim_threshold: _Unit = 0.6
    alpha: float = Field(0.5, gt=0)
    k0: float = Field(1.0, gt=0)
    delta_bounds: Annotated[
        tuple[float, float], _rule(lambda v: v[0] < 0 < v[1], "needs lower < 0 < upper")
    ] = (-0.25, 0.15)
    confidence_bounds: Annotated[
        tuple[float, float], _rule(lambda v: 0 < v[0] < v[1] < 1, "needs 0 < lower < upper < 1")
    ] = (0.05, 0.95)


class PromoteConfig(_Section):
    min_passes: int = Field(3, ge=1)
    min_runs: int = Field(2, ge=1)
    min_pass_lb: _Open = 0.7


class LoraConfig(_Section):
    min_pass_lb: _Open = 0.8
    val_fraction: _Open = 0.1
    golden_exclusion_cosine: _Open = 0.90


class ProceduralConfig(_Section):
    """Procedural memory; demote_pass_lb < promote.min_pass_lb <= lora.min_pass_lb."""

    promote: PromoteConfig = Field(default_factory=PromoteConfig)
    demote_pass_lb: _Open = 0.5
    lora: LoraConfig = Field(default_factory=LoraConfig)

    @model_validator(mode="after")
    def _ordered(self) -> Self:
        _require(
            self.demote_pass_lb < self.promote.min_pass_lb <= self.lora.min_pass_lb,
            "needs demote_pass_lb < promote.min_pass_lb <= lora.min_pass_lb",
        )
        return self


class ChatMemoryConfig(_Section):
    """Chat session memory."""

    last_messages: int = Field(10, ge=1, le=50)
    summary_every_turns: int = Field(6, ge=1, le=50)
    summary_max_chars: int = Field(6000, ge=500, le=20_000)
    correction_min_confidence: _Unit = 0.7


class MemoryConfig(_Section):
    """Validated `cfg.memory` plus `injection_patterns` (filled by the spec 10 loader)."""

    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    recall: RecallConfig = Field(default_factory=RecallConfig)
    write: WriteConfig = Field(default_factory=WriteConfig)
    episodic: EpisodicConfig = Field(default_factory=EpisodicConfig)
    outcome: OutcomeConfig = Field(default_factory=OutcomeConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)
    procedural: ProceduralConfig = Field(default_factory=ProceduralConfig)
    chat: ChatMemoryConfig = Field(default_factory=ChatMemoryConfig)
    injection_patterns: Annotated[
        tuple[Annotated[str, AfterValidator(_check_pattern)], ...], Field(max_length=MAX_PATTERNS)
    ] = ()
