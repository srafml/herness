"""Models of `config/eval.yaml` (design 11 §7; U11-52, U11-74; R-03).

`EvalConfig` is the root model spec 10 mounts at `cfg.eval` (T10-03). This module
imports only the standard library, pydantic and `herness.core.errors` (ENG §2.1
settings exception, R-03, contract `settings modules are leaves`).
"""

from __future__ import annotations

from typing import Annotated, Final, Literal, get_args

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError

from herness.core.errors import ConfigError

type RepeatPhase = Literal["fast", "standard", "deep"]

_FLOOR_KEYS: Final = frozenset({"local-fast", "local-standard", "local-deep", "hybrid", "premium"})

_Fraction = Annotated[float, Field(ge=0, le=1)]
_NonNegFloat = Annotated[float, Field(ge=0)]
_PosFloat = Annotated[float, Field(gt=0)]
_NonEmptyStr = Annotated[str, Field(min_length=1)]
_RepeatCount = Annotated[int, Field(ge=1, le=10)]


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def _repeat_keys(value: dict[RepeatPhase, int]) -> dict[RepeatPhase, int]:
    wanted = set(get_args(RepeatPhase))
    missing = wanted - set(value)
    _require(not missing, f"repeat must have every phase: {', '.join(sorted(missing))}")
    return value


def _floor_keys(value: dict[str, float]) -> dict[str, float]:
    bad = set(value) - _FLOOR_KEYS
    _require(not bad, f"correctness_floor has unknown keys: {', '.join(sorted(bad))}")
    return value


class _Model(BaseModel):
    """Base of every model here: closed, strict and immutable (U11-52)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class JudgeSettings(_Model):
    """`eval.judge`: the rubric-judge client (design 11 §7, spec 05 §5.1.3)."""

    profile: _NonEmptyStr
    temperature: _NonNegFloat
    cache_dir: _NonEmptyStr


class Thresholds(_Model):
    """`eval.thresholds`: gate criteria for correctness, tools, latency and cost."""

    unsupported_number_rate_max: _NonNegFloat
    correctness_drop_max_pp: _NonNegFloat
    correctness_floor: Annotated[dict[str, _Fraction], AfterValidator(_floor_keys)]
    tool_success_min: dict[str, _Fraction]
    latency_p95_ratio_max: _PosFloat
    cost_ratio_max: _PosFloat
    bench_regression_max: _NonNegFloat


class ClassifierSettings(_Model):
    """`eval.classifier`: synthetic gate-recompute check."""

    gate_recompute_tolerance: _Fraction
    synthetic_sample: int = Field(ge=100, le=100_000)
    bootstrap: int = Field(ge=100, le=10_000)


class EvalConfig(_Model):
    """Root of `config/eval.yaml`, mounted by spec 10 at `cfg.eval` (U11-52)."""

    suite: _NonEmptyStr
    judge: JudgeSettings
    repeat: Annotated[dict[RepeatPhase, _RepeatCount], AfterValidator(_repeat_keys)]
    thresholds: Thresholds
    classifier: ClassifierSettings
    baseline: _NonEmptyStr


def load_eval_config(raw: object) -> EvalConfig:
    """Validate `raw` (as loaded from `config/eval.yaml`) into `EvalConfig`."""
    try:
        return EvalConfig.model_validate(raw)
    except ValidationError as exc:
        msg = "eval config failed validation"
        raise ConfigError(msg, hint=str(exc)) from exc
