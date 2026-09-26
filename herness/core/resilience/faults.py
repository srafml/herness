"""Fault plans and the in-process fault hook (U08-33, U08-34; design 08 §5.13, R-40).

`fault_point` is inert unless `HERNESS_FAULTS` names a JSON plan AND `HERNESS_ENV=test`; in
any other environment the plan file is never opened and one WARNING is logged (TH08-06).
Plans are JSON only (no YAML parser here), at most 64 KiB and 100 rules, with closed point,
action and error-class sets.
"""

from __future__ import annotations

import importlib
import ipaddress
import json
import os
import random
import re
import signal
import typing
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Final, Self

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator, model_validator

from herness.core import errors as e
from herness.core import time as clock
from herness.core.errors import ConfigError, HernessError
from herness.core.logging import get_logger
from herness.core.resilience._state import NOT_LOADED, process_state
from herness.core.types import ServiceName

# fmt: off
# The 18 points of design 08 §5.13; the only registry of fault point names (R-40).
NAMED_POINTS: Final[frozenset[str]] = frozenset((
    "http.page", "connector.before_watermark", "llm.call", "llm.output", "sql.query",
    "decider.batch", "embed.batch", "enrich.after_batch_write", "build.mid_sql",
    "pipeline.before_promote", "swarm.after_task_claim", "swarm.after_finding_write",
    "verifier.mid_batch", "sqlite.write", "job.after_claim", "job.before_complete",
    "gpu.after_stop", "gpu.after_start",
))
LABEL_KEYS: Final = ("model", "source", "role", "kind")
MAX_PLAN_BYTES: Final = 65_536
MAX_RULES: Final = 100
MAX_DELAY_S: Final = 600.0
_PLAIN_ACTIONS: Final = frozenset({"timeout", "http_429", "http_503", "malformed_json", "kill"})
# The 18 leaf classes of spec 00 §7 incl. NotFound (R-19); spec-local subclasses are excluded.
_LEAF_ERRORS: Final[Mapping[str, type[HernessError]]] = {
    name: getattr(e, name)
    for name in (
        "SourceUnavailable", "ModelUnavailable", "StoreBusy", "RateLimited", "CircuitOpen",
        "OutputValidationError", "ToolInputError", "QueryError", "ModelRefused",
        "PolicyViolation", "ReportContractError", "NotFound", "ConfigError", "AuthError",
        "SchemaViolation", "BudgetExceeded", "PermissionDenied", "EgressBlocked",
    )
}
# fmt: on
_SERVICES: Final[frozenset[str]] = frozenset(typing.get_args(ServiceName.__value__))
_DELAY_RE: Final = re.compile(r"[0-9]{1,3}(\.[0-9]{1,6})?")
_log = get_logger("resilience")


def _check_action(action: str) -> None:
    verb, _, arg = action.partition(":")
    ok = action in _PLAIN_ACTIONS
    if verb == "error":
        ok = arg in _LEAF_ERRORS
    elif verb == "delay":
        ok = _DELAY_RE.fullmatch(arg) is not None and 0 < float(arg) <= MAX_DELAY_S
    elif verb == "kill_service":
        ok = arg in _SERVICES
    if not ok:
        msg = f"unknown fault action {action[:40]!r}"
        raise ValueError(msg)


class FaultRule(BaseModel):
    """One validated rule of a fault plan (U08-33)."""

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    point: str
    action: str
    nth: int | None = None
    count: int | None = None
    p: float | None = None
    seed: int | None = None
    retry_after: float | None = None
    model: str | None = None
    source: str | None = None
    role: str | None = None
    kind: str | None = None

    @field_validator("point")
    @classmethod
    def _known_point(cls, value: str) -> str:
        if value not in NAMED_POINTS:
            msg = f"unknown fault point {value[:40]!r}"
            raise ValueError(msg)
        return value

    @field_validator("action")
    @classmethod
    def _known_action(cls, value: str) -> str:
        _check_action(value)
        return value

    @model_validator(mode="after")
    def _selectors(self) -> Self:
        problems = [
            (self.nth is not None and self.nth < 1, "nth must be >= 1"),
            (self.count is not None and self.count < 1, "count must be >= 1"),
            (self.nth is not None and self.count is not None, "at most one of nth and count"),
            (self.p is not None and not 0 < self.p <= 1, "p must be in (0, 1]"),
            (self.p is not None and self.seed is None, "p requires seed"),
            (self.retry_after is not None and self.action != "http_429", "retry_after needs 429"),
            (self.retry_after is not None and not self.retry_after >= 0, "retry_after >= 0"),
        ]
        for failed, reason in problems:
            if failed:
                raise ValueError(reason)
        return self

    def matches(self, labels: Mapping[str, str]) -> bool:
        """True when every label filter set on this rule equals the call's label."""
        wanted = {key: getattr(self, key) for key in LABEL_KEYS}
        return all(value is None or labels.get(key) == value for key, value in wanted.items())


@dataclass(slots=True)
class FaultPlan:
    """A loaded plan: rules plus one call counter and one seeded RNG (or None) per rule."""

    rules: tuple[FaultRule, ...]
    counters: list[int]
    rngs: list[random.Random | None]


def _invalid(path: Path, reason: str) -> ConfigError:
    return ConfigError(f"invalid fault plan: {path.name}: {reason}")


def _read_plan(path: Path) -> bytes:
    if path.is_symlink():
        raise _invalid(path, "symlink not allowed")
    try:
        resolved = path.resolve(strict=True)
        if not resolved.is_file():
            raise _invalid(path, "not a regular file")
        if resolved.suffix.lower() != ".json":
            raise _invalid(path, "fault plans are JSON only")
        with resolved.open("rb") as handle:  # read one byte past the limit, never more
            data = handle.read(MAX_PLAN_BYTES + 1)
    except OSError as exc:
        raise _invalid(path, f"unreadable ({type(exc).__name__})") from None
    if len(data) > MAX_PLAN_BYTES:
        raise _invalid(path, f"larger than {MAX_PLAN_BYTES} bytes")
    return data


def _no_constant(name: str) -> object:
    msg = f"{name} not allowed"
    raise ValueError(msg)


def load_fault_plan(path: Path) -> FaultPlan:
    """Load and validate a JSON fault plan (U08-33); any violation → `ConfigError`."""
    data = _read_plan(path)
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_no_constant)
    except (UnicodeDecodeError, ValueError):
        raise _invalid(path, "not valid JSON") from None
    if not isinstance(document, list) or len(document) > MAX_RULES:
        raise _invalid(path, f"must be a list of at most {MAX_RULES} rules")
    rules: list[FaultRule] = []
    for index, item in enumerate(document):
        if not isinstance(item, dict):
            raise _invalid(path, f"rule {index}: not a mapping")
        try:
            rules.append(FaultRule.model_validate(item))
        except ValidationError as exc:
            first = exc.errors(include_input=False, include_url=False)[0]
            where = ".".join(str(part) for part in first["loc"]) or "rule"
            raise _invalid(path, f"rule {index}: {where}: {first['msg']}") from None
    rngs = [None if rule.p is None else random.Random(rule.seed) for rule in rules]  # noqa: S311
    return FaultPlan(tuple(rules), [0] * len(rules), rngs)


def _ensure_loaded() -> FaultPlan | None:
    state = process_state()
    plan = state.fault_plan
    if plan is not NOT_LOADED:
        return plan
    with state.lock:
        if state.fault_plan is not NOT_LOADED:
            return state.fault_plan
        source = os.environ.get("HERNESS_FAULTS")
        env = os.environ.get("HERNESS_ENV")
        if not source or env != "test":
            state.fault_plan = None
            if source:  # R-40: a plan outside HERNESS_ENV=test is never opened
                _log.warning("resilience.faults.ignored", env=(env or "unset")[:32])
            return None
        loaded = load_fault_plan(Path(source))
        state.fault_plan = loaded
        state.faults_enabled = True
    _log.warning("resilience.faults.enabled", plan=Path(source).name, rules=len(loaded.rules))
    return loaded


def _fires(plan: FaultPlan, index: int) -> bool:
    rule = plan.rules[index]
    plan.counters[index] += 1
    calls = plan.counters[index]
    if rule.nth is None and rule.count is None and rule.p is None:
        return True
    rng = plan.rngs[index]
    return (
        (rule.nth is not None and calls == rule.nth)
        or (rule.count is not None and calls <= rule.count)
        or (rng is not None and rule.p is not None and rng.random() < rule.p)
    )


def _select(plan: FaultPlan, name: str, labels: Mapping[str, str]) -> FaultRule | None:
    with process_state().lock:
        for index, rule in enumerate(plan.rules):
            if rule.point == name and rule.matches(labels) and _fires(plan, index):
                return rule
    return None


def _family(point: str, message: str) -> HernessError:
    if point.startswith("http."):
        return e.SourceUnavailable(message)
    if point.startswith("sqlite."):
        return e.StoreBusy(message)
    return e.ModelUnavailable(message)


def _injected(class_name: str, point: str) -> HernessError:
    cls = _LEAF_ERRORS[class_name]
    if cls is e.CircuitOpen:  # the only leaf with required attributes
        return e.CircuitOpen("fault: injected", key=f"fault:{point}", retry_at=clock.now())
    return cls("fault: injected")


def _kill_process() -> None:
    _log.critical("resilience.faults.kill")
    os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))


def _is_loopback_url(url: object) -> bool:
    if not isinstance(url, str):
        return False
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname or ""
    try:
        loopback = host == "localhost" or ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = False
    return loopback and parts.scheme in {"http", "https"}


def _stub_url(service: str) -> str | None:
    """The stub URL of `service` from `HERNESS_STUB_SERVICES` (JSON object), or None."""
    raw = os.environ.get("HERNESS_STUB_SERVICES")
    if not raw:
        return None
    msg = "HERNESS_STUB_SERVICES must map service names to loopback http(s) URLs"
    try:
        stubs = json.loads(raw)
    except ValueError:
        raise ConfigError(msg) from None
    url = stubs.get(service) if isinstance(stubs, dict) else ""
    if url is not None and not _is_loopback_url(url):
        raise ConfigError(msg)
    return url  # a str: checked by _is_loopback_url


def _kill_service(service: str) -> None:
    url = _stub_url(service)
    if url is not None:
        try:
            egress = importlib.import_module("herness.core.egress")
        except ModuleNotFoundError as exc:  # impl 10 T10-18 not in the tree yet
            if exc.name != "herness.core.egress":
                raise
        else:
            with egress.loopback_http_client(url, timeout_s=5) as client:
                client.post("/__control/kill").raise_for_status()
            return
    hook = process_state().kill_service_hook
    if hook is not None:
        hook(service)
        return
    _log.warning("resilience.faults.no_service_hook", service=service)


def _error_for(rule: FaultRule, verb: str, arg: str) -> HernessError:
    msg = f"fault: {verb}"
    if verb == "error":
        return _injected(arg, rule.point)
    if verb == "http_429":
        return e.RateLimited(msg, retry_after=rule.retry_after)
    if verb == "malformed_json":
        return e.OutputValidationError(msg)  # U08-35 treats it as an invalid reply
    if verb == "timeout" and rule.point.startswith("sql."):
        return e.QueryError(msg, timeout=True)  # `timeout` is carried as err.context["timeout"]
    return _family(rule.point, msg)  # timeout elsewhere, http_503


def _apply(rule: FaultRule) -> None:
    verb, _, arg = rule.action.partition(":")
    if verb == "kill":
        _kill_process()
    elif verb == "delay":
        process_state().sleep(float(arg))
    elif verb == "kill_service":
        _kill_service(arg)
    else:
        raise _error_for(rule, verb, arg)


def fault_point(name: str, **labels: str) -> None:
    """In-process fault hook (U08-34): a no-op unless a plan is enabled under `HERNESS_ENV=test`.

    With a plan, the first rule on `name` whose label filters match and whose selector fires
    applies its action (raise, sleep, kill or kill a service).
    """
    plan = _ensure_loaded()
    if plan is None:
        return
    if name not in NAMED_POINTS or not set(labels) <= set(LABEL_KEYS):
        msg = f"unknown fault point or label: {name[:40]}"
        raise ConfigError(msg)
    rule = _select(plan, name, labels)
    if rule is not None:
        _apply(rule)
