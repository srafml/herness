"""08-owned shared data types: closed literal sets, `JobSpec`, `JobOutcome` and
`MetricSample` (design 08 §3.1, §3.4, §3.6; R-01, R-02).

Re-exported from `herness.core.types`. `JobContext` and the resilience/jobs behavioral
classes live in `herness.core.resilience` and `herness.core.jobs`, never here (R-02).
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationInfo, field_validator

from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json

_IDEM_KEY_RE: Final = r"^[A-Za-z0-9_:.\-/]+$"
_METRIC_NAME_RE: Final = r"^herness_[a-z][a-z0-9]*(_[a-z0-9]+)+$"
_COMPONENT_RE: Final = r"^[a-z][a-z0-9_]{0,31}$"
_LABEL_KEY_RE: Final = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
_LABEL_VALUE_RE: Final = re.compile(r"^[A-Za-z0-9_.:\-]{1,64}$")
_MAX_RESULT_BYTES: Final = 1_048_576
_MAX_LABEL_BYTES: Final = 1024
_MAX_LABELS: Final = 6

type GpuClass = Literal["none", "reasoning", "decider", "large"]
type JobKind = Literal[
    "sync",
    "reconcile",
    "build_pipeline",
    "distill",
    "review",
    "chat",
    "outcome_measure",
    "memory_maintenance",
    "maintenance",
    "eval",
]
type ServiceName = Literal["vllm-reasoning", "openjev", "llamacpp-large"]
type ChatMode = Literal["live", "small_model", "defer", "cloud"]
type BreakerState = Literal["closed", "open", "half_open"]
type PolicyName = Literal[
    "source_http_page",
    "llm_local",
    "llm_large",
    "llm_cloud",
    "decider_local",
    "decider_cloud",
    "embed_batch",
    "tool_store",
    "warehouse_read",
    "sqlite_write",
    "gpu_health",
]


def _require_aware(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        msg = "must be timezone-aware"
        raise ValueError(msg)
    return value


def _canonical_size(value: object) -> int:
    try:
        encoded = canonical_json(value)
    except SchemaViolation as exc:
        msg = "value is not canonical-JSON encodable"
        raise ValueError(msg) from exc
    return len(encoded.encode("utf-8"))


class JobSpec(BaseModel):
    """Request to create one job (design 08 §3.4)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    kind: JobKind
    payload: dict[str, JsonValue]
    gpu_class: GpuClass
    priority: int | None = Field(default=None, ge=0, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=20)
    scheduled_for: datetime | None = None
    idem_key: str | None = Field(default=None, max_length=200, pattern=_IDEM_KEY_RE)

    @field_validator("scheduled_for")
    @classmethod
    def _scheduled_for_aware(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_aware(value)


class JobOutcome(BaseModel):
    """Handler return value; `yield` requeues without an attempt charge (design 08 §3.4)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["done", "yield"]
    result: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("result")
    @classmethod
    def _result_size(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        if _canonical_size(value) > _MAX_RESULT_BYTES:
            msg = "result exceeds 1 MiB of canonical JSON"
            raise ValueError(msg)
        return value


class MetricSample(BaseModel):
    """One `metric_sample` row, as handed to `record_metric_samples` (TH08-02)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    ts: datetime
    name: str = Field(pattern=_METRIC_NAME_RE)
    kind: Literal["counter", "gauge", "histogram"]
    value: float
    labels: dict[str, str] = Field(default_factory=dict)
    component: str = Field(pattern=_COMPONENT_RE)

    @field_validator("ts")
    @classmethod
    def _ts_aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @field_validator("value")
    @classmethod
    def _value_valid(cls, value: float, info: ValidationInfo) -> float:
        if not math.isfinite(value):
            msg = "value must be finite"
            raise ValueError(msg)
        if info.data.get("kind") in ("counter", "histogram") and value < 0:
            msg = "value must be >= 0 for counter and histogram"
            raise ValueError(msg)
        return value

    @field_validator("labels")
    @classmethod
    def _labels_valid(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_LABELS:
            msg = f"labels has more than {_MAX_LABELS} keys"
            raise ValueError(msg)
        for key, val in value.items():
            if not _LABEL_KEY_RE.fullmatch(key) or not _LABEL_VALUE_RE.fullmatch(val):
                msg = f"invalid label {key!r}"
                raise ValueError(msg)
        # Defense in depth (TH08-02): unreachable while the key/value regexes and the
        # 6-key cap above hold (6 * (32 + 64 + 5) + 7 = 613 bytes), kept for when either
        # bound changes.
        if _canonical_size(value) > _MAX_LABEL_BYTES:  # pragma: no cover
            msg = "labels exceeds 1024 bytes of canonical JSON"
            raise ValueError(msg)
        return value
