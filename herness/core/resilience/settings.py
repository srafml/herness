"""Models of `config/resilience.yaml` (design 08 §7; U08-06, U08-07; R-03).

`ResilienceConfig` is the root spec 10 mounts at `cfg.resilience`; its `resilience` key is
`ResilienceSection` and its `schedule` key is `ScheduleSection`. This module imports only the
standard library, pydantic, `herness.core.types` and `herness.core.errors` (R-03, contract
`resilience-settings-light`). Cron strings are checked for shape only; the full parse and the
week coverage of windows are U08-99's job. Service URLs must be loopback (TH08-12) and
service names come from the `ServiceName` allowlist (TH08-01).
"""

from __future__ import annotations

import re
from typing import Annotated, Final, Literal, Self, get_args
from urllib.parse import urlsplit

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    model_validator,
)

from herness.core.types import GpuClass, JobKind, PolicyName, ServiceName

_NAME_RE: Final = r"^[a-z][a-z0-9_]{0,31}$"
_HHMM_RE: Final = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"
_SECRET_REF_RE: Final = r"^secret:[A-Z][A-Z0-9_]{0,63}$"  # noqa: S105 - a pattern, not a secret
_CRON_FIELD_RE: Final = re.compile(r"[0-9A-Za-z*/,\-]+")
_CATCH_UP_RE: Final = re.compile(r"([0-9]{1,9})(m|h|d)")
_CRON_FIELDS: Final = 5
_LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1"})
_CATCH_UP_MAX_S: Final = 7 * 86400
_UNIT_SECONDS: Final = {"m": 60, "h": 3600, "d": 86400}
_JOB_KINDS: Final = frozenset(get_args(JobKind.__value__))
_RETRY_POLICIES: Final = frozenset(get_args(PolicyName.__value__)) - {"gpu_health"}
_RESERVED_JOB_PREFIXES: Final = ("sync.", "reconcile.")

type Weekday = Literal["MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN"]
type ServiceClass = Literal["reasoning", "decider", "large"]
type ChatFallback = Literal["small_model", "defer", "cloud"]

_PosFloat = Annotated[float, Field(gt=0, allow_inf_nan=False)]
_NonNegFloat = Annotated[float, Field(ge=0, allow_inf_nan=False)]
_PosInt = Annotated[int, Field(gt=0)]
_Name = Annotated[str, Field(pattern=_NAME_RE)]
_HhMm = Annotated[str, Field(pattern=_HHMM_RE)]


def _require(ok: bool, msg: str) -> None:
    if not ok:
        raise ValueError(msg)


def _cron_shape(value: str) -> str:
    fields = value.split()
    ok = len(fields) == _CRON_FIELDS and all(_CRON_FIELD_RE.fullmatch(f) for f in fields)
    _require(ok, "cron must have five whitespace-separated fields of [0-9A-Za-z*/,-]")
    return value


def _catch_up(value: str) -> str:
    match = _CATCH_UP_RE.fullmatch(value)
    if match is None:
        msg = "catch_up_max must look like <n>m, <n>h or <n>d"
        raise ValueError(msg)
    seconds = int(match[1]) * _UNIT_SECONDS[match[2]]  # plain int: no timedelta overflow
    _require(seconds <= _CATCH_UP_MAX_S, "catch_up_max must be at most 7 days")
    return value


def _no_nul(value: str) -> str:
    _require("\x00" not in value, "argument must not contain NUL")
    return value


def _unique[T](values: list[T]) -> list[T]:
    _require(len(set(values)) == len(values), "items must be unique")
    return values


def _loopback_url(value: str) -> str:
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        msg = "service url is malformed"
        raise ValueError(msg) from exc
    loopback = parts.hostname in _LOOPBACK_HOSTS and parts.username is None
    ok = parts.scheme == "http" and loopback and port is not None and port > 0
    _require(ok, "service url must be http on 127.0.0.1, localhost or ::1 with an explicit port")
    return value


def _policy_keys[V](value: dict[PolicyName, V]) -> dict[PolicyName, V]:
    wrong = ", ".join(sorted(set(value) ^ _RETRY_POLICIES))
    _require(not wrong, f"policies must be every PolicyName but gpu_health; wrong keys: {wrong}")
    return value


def _health_path(value: str) -> str:
    _require(value.startswith("/"), "health path must start with '/'")
    return value


class _Model(BaseModel):
    """Base of every model here: closed, strict and immutable (U08-06, U08-07)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


_Cron = Annotated[str, AfterValidator(_cron_shape)]
_CatchUp = Annotated[str, AfterValidator(_catch_up)]
_Argv = Annotated[list[Annotated[str, AfterValidator(_no_nul)]], Field(min_length=1)]


# --- resilience (U08-06) ---------------------------------------------------------------


class PolicySettings(_Model):
    """One retry policy (design 08 §5.2)."""

    attempts: int = Field(ge=1, le=100)
    base_s: _PosFloat
    cap_s: _PosFloat
    max_elapsed_s: _PosFloat
    timeout_s: _PosFloat | None = None
    connect_timeout_s: _PosFloat | None = None
    retry_after_cap_s: _NonNegFloat = 0

    @model_validator(mode="after")
    def _cap_not_below_base(self) -> Self:
        _require(self.cap_s >= self.base_s, "cap_s must be >= base_s")
        return self


class JobBackoffSettings(_Model):
    """Backoff between job attempts (design 08 §5.7)."""

    base_s: _PosFloat = 60
    cap_s: _PosFloat = 3600


class RetrySettings(_Model):
    """`resilience.retry`: every configurable policy plus job backoff."""

    policies: Annotated[dict[PolicyName, PolicySettings], AfterValidator(_policy_keys)]
    job_backoff: JobBackoffSettings
    retry_after_max_s: _PosFloat = 86400


class BreakerSettings(_Model):
    """One circuit breaker family (design 08 §5.3)."""

    failure_threshold: int = Field(ge=1)
    cooldown_s: _PosFloat
    cooldown_max_s: _PosFloat

    @model_validator(mode="after")
    def _max_not_below_cooldown(self) -> Self:
        _require(self.cooldown_max_s >= self.cooldown_s, "cooldown_max_s must be >= cooldown_s")
        return self


class BreakersSection(_Model):
    """`resilience.breakers`."""

    source: BreakerSettings
    model: BreakerSettings
    decider: BreakerSettings
    restart_max_per_hour: int = Field(default=1, ge=0)


class FallbackSettings(_Model):
    """`resilience.fallback`: repair budget and decider chains per profile."""

    max_repairs: int = Field(default=2, ge=0, le=5)
    decider_chain: dict[_Name, Annotated[list[_Name], Field(min_length=1), AfterValidator(_unique)]]


class LoopSettings(_Model):
    """`resilience.loop`."""

    checkpoint_min_interval_s: _NonNegFloat = 5
    stop_on_signal_no: int = Field(default=2, ge=1)


class TaskSettings(_Model):
    """`resilience.tasks`."""

    max_task_attempts: int = Field(default=3, ge=1)


class JobsSettings(_Model):
    """`resilience.jobs`: queue, lease and supervisor timings (design 08 §5.7, §5.9)."""

    lease_s: _PosInt = 300
    heartbeat_s: _PosInt = 30
    reaper_interval_s: _PosInt = 30
    tick_s: _PosFloat = 2
    cancel_grace_s: _PosInt = 60
    shutdown_grace_s: _PosInt = 120
    stall_timeout_s: _PosInt = 1800
    stall_timeout_large_s: _PosInt = 7200
    cpu_slots: int = Field(default=2, ge=0, le=16)
    exclusive_kinds: list[JobKind]
    max_attempts: dict[JobKind, Annotated[int, Field(ge=1, le=20)]]

    @model_validator(mode="after")
    def _check(self) -> Self:
        missing = _JOB_KINDS - set(self.max_attempts)
        _require(not missing, f"max_attempts misses job kinds: {', '.join(sorted(missing))}")
        _require(self.heartbeat_s * 3 < self.lease_s, "heartbeat_s * 3 must be < lease_s")
        return self


class HealthCheck(_Model):
    """Health probe of one service; `bearer_secret` is a secret reference (R-53)."""

    path: Annotated[str, AfterValidator(_health_path)]
    bearer_secret: Annotated[str, Field(pattern=_SECRET_REF_RE)] | None = None


class ServiceSettings(_Model):
    """One compose service on a loopback port (R-51; TH08-12)."""

    url: Annotated[str, AfterValidator(_loopback_url)]
    health: HealthCheck
    start_timeout_s: _PosInt
    start_on_entry: bool = True


class GpuClassSettings(_Model):
    """Services of one GPU class (compose profile = class name)."""

    services: dict[ServiceName, ServiceSettings]


class GpuSettings(_Model):
    """`resilience.gpu`: compose control, VRAM check and the class catalogue (design 08 §5.8)."""

    compose_cmd: _Argv
    compose_file: Annotated[str, AfterValidator(_no_nul)]
    vram_check_cmd: _Argv
    vram_free_threshold_mb: int = Field(default=2000, ge=0)
    stop_timeout_s: _PosInt = 120
    warmup_timeout_s: _PosInt = 120
    classes: dict[ServiceClass, GpuClassSettings]

    @model_validator(mode="after")
    def _check_classes(self) -> Self:
        missing = set(get_args(ServiceClass.__value__)) - set(self.classes)
        _require(not missing, f"gpu.classes misses: {', '.join(sorted(missing))}")
        seen: set[str] = set()
        for cls in self.classes.values():
            _require(not seen & set(cls.services), "a service appears in more than one class")
            seen |= set(cls.services)
        return self


class ResilienceSection(_Model):
    """The `resilience` key of `config/resilience.yaml` (U08-06)."""

    retry: RetrySettings
    breakers: BreakersSection
    fallback: FallbackSettings
    loop: LoopSettings = Field(default_factory=LoopSettings)
    tasks: TaskSettings = Field(default_factory=TaskSettings)
    jobs: JobsSettings
    gpu: GpuSettings


# --- schedule (U08-07) -------------------------------------------------------------------


class WindowSpec(_Model):
    """One schedule window; `days` names the day the window starts (None = every day)."""

    name: _Name
    start: _HhMm
    end: _HhMm
    days: list[Weekday] | None = None
    classes: Annotated[list[ServiceClass], Field(min_length=1), AfterValidator(_unique)]
    preload: ServiceClass | None = None
    hard_start: bool = False
    overrun_max_min: int = Field(default=0, ge=0, le=240)

    @model_validator(mode="after")
    def _check(self) -> Self:
        _require(self.start != self.end, "window start and end must differ")
        ok = self.preload is None or self.preload in self.classes
        _require(ok, "preload must be one of the window classes")
        return self


class ChatSchedule(_Model):
    """`schedule.chat`: chat policy outside hours or without the reasoning class."""

    off_hours: ChatFallback = "small_model"
    in_hours_unavailable: ChatFallback = "small_model"


class RekeySchedule(_Model):
    """`schedule.rekey`: planned rekey slot."""

    cron: _Cron = "0 19 * * SAT"
    min_notice_h: int = Field(default=12, ge=0, le=168)


class ChainStep(_Model):
    """One job of a scheduled chain; `priority` None means `DEFAULT_PRIORITY[kind]`."""

    kind: JobKind
    gpu_class: GpuClass
    payload: dict[str, JsonValue] = Field(default_factory=dict)
    skip_on: list[Weekday] = Field(default_factory=list)
    enabled: bool = True
    priority: int | None = Field(default=None, ge=0, le=100)


def _job_name(value: str) -> str:
    reserved = value == "maintenance" or value.startswith(_RESERVED_JOB_PREFIXES)
    _require(not reserved, "scheduled job name is reserved")
    return value


class ScheduledJob(_Model):
    """One cron-driven chain of `schedule.jobs`."""

    name: Annotated[str, Field(pattern=_NAME_RE), AfterValidator(_job_name)]
    cron: _Cron
    catch_up_max: _CatchUp
    job: ChainStep
    then: list[ChainStep] = Field(default_factory=list, max_length=10)


class MaintenanceSchedule(_Model):
    """`schedule.maintenance` (its time comes from spec 10 `backup.nightly_at`)."""

    catch_up_max: _CatchUp = "12h"


class ScheduleSection(_Model):
    """The `schedule` key of `config/resilience.yaml` (U08-07)."""

    windows: list[WindowSpec]
    preempt_grace_min: int = Field(default=15, ge=0)
    batch_in_chat_min_priority: int = Field(default=70, ge=0, le=100)
    chat: ChatSchedule = Field(default_factory=ChatSchedule)
    rekey: RekeySchedule = Field(default_factory=RekeySchedule)
    jobs: list[ScheduledJob]
    maintenance: MaintenanceSchedule = Field(default_factory=MaintenanceSchedule)

    @model_validator(mode="after")
    def _unique_job_names(self) -> Self:
        names = [job.name for job in self.jobs]
        _require(len(set(names)) == len(names), "duplicate scheduled job name")
        return self


class ResilienceConfig(_Model):
    """Root of `config/resilience.yaml`, mounted by spec 10 at `cfg.resilience` (U08-07)."""

    resilience: ResilienceSection
    schedule: ScheduleSection

    @model_validator(mode="after")
    def _classes_known(self) -> Self:
        known = set(self.resilience.gpu.classes)
        for i, window in enumerate(self.schedule.windows):
            wanted = [*window.classes, *([window.preload] if window.preload else [])]
            msg = f"schedule.windows[{i}].classes: class not in resilience.gpu.classes"
            _require(set(wanted) <= known, msg)
        for i, job in enumerate(self.schedule.jobs):
            steps = [("job", job.job)] + [(f"then[{n}]", step) for n, step in enumerate(job.then)]
            for where, step in steps:
                msg = f"schedule.jobs[{i}].{where}.gpu_class: not in resilience.gpu.classes"
                _require(step.gpu_class in {"none", *known}, msg)
        return self
