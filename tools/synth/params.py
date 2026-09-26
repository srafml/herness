"""Scale presets, generator parameters and the params hash (U11-01, U11-02).

Defaults follow design §5.1.1, §5.1.3, §5.1.4 and §5.1.6 plus delta DD11-05, with the
values the generator units of impl 11 §3.1 fix. A params file (`--params`) is read with
`yaml.safe_load` under a 256 KB cap (TH11-08) and deep-merged over the defaults: maps
merge, lists replace. Every failure is a `SynthUsageError` naming the key path.
"""

import dataclasses
import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Any, Final, Literal, NoReturn

import yaml
from pydantic import AfterValidator, BaseModel, ConfigDict, ValidationError

from herness.core.errors import ConfigError
from tools.synth.param_groups import (
    AckParams,
    ArrivalParams,
    ChangeParams,
    DirtyRates,
    EventParams,
    ImpactParams,
    IncidentParams,
    JiraParams,
    MetricDailyParams,
    MttrParams,
    OrgParams,
    PiiParams,
    PriorityParams,
    ProblemParams,
    ReassignParams,
    SlaParams,
    TextParams,
    check_zone,
)


class SynthUsageError(ConfigError):
    """A generator argument or params file is invalid (exit 3, R-46)."""


@dataclasses.dataclass(frozen=True, slots=True)
class ScalePreset:
    """Row counts and span of one scale preset (design §5.1.1)."""

    name: Literal["tiny", "small", "full"]
    orgs: int
    teams: int
    services: int
    incidents: int
    changes: int
    problems: int
    events: int
    jira_issues: int
    metric_services: int
    span_days: int | None  # None = use --start/--end
    catalog_class: Literal["tiny", "standard"]


SCALE_PRESETS: Final[dict[str, ScalePreset]] = {
    "tiny": ScalePreset("tiny", 3, 12, 20, 1_200, 250, 40, 400, 150, 20, 90, "tiny"),
    "small": ScalePreset(
        "small", 12, 150, 400, 100_000, 10_000, 1_500, 40_000, 4_000, 400, None, "standard"
    ),
    "full": ScalePreset(
        "full", 12, 150, 400, 5_000_000, 500_000, 60_000, 2_000_000, 200_000, 400, None, "standard"
    ),
}

SOURCES: Final = ("servicenow", "jira", "monitoring", "files")
PARAMS_FILE_MAX_BYTES: Final = 256 * 1024
_MAX_NODES: Final = 100_000  # caps YAML alias expansion while merging (TH11-08)
_MAX_DEPTH: Final = 32
# Set by CLI arguments; a params file may override only parameter groups.
_ARGUMENT_KEYS: Final = ("scale", "preset", "start", "end", "sources", "dirty", "fetch_mode")


class SynthParams(BaseModel):
    """Every generator parameter; build it with `load_params`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scale: Literal["tiny", "small", "full"]
    preset: ScalePreset
    start: date
    end: date
    sources: tuple[Literal["servicenow", "jira", "monitoring", "files"], ...]
    dirty: Literal["none", "default", "heavy"]
    fetch_mode: Literal["initial", "daily"]
    org: OrgParams = OrgParams()
    incident: IncidentParams = IncidentParams()
    priority: PriorityParams = PriorityParams()
    arrival: ArrivalParams = ArrivalParams()
    ack: AckParams = AckParams()
    mttr: MttrParams = MttrParams()
    reassign: ReassignParams = ReassignParams()
    sla: SlaParams = SlaParams()
    impact: ImpactParams = ImpactParams()
    change: ChangeParams = ChangeParams()
    problem: ProblemParams = ProblemParams()
    event: EventParams = EventParams()
    metric_daily: MetricDailyParams = MetricDailyParams()
    jira: JiraParams = JiraParams()
    text: TextParams = TextParams()
    pii: PiiParams = PiiParams()
    dirty_rates: DirtyRates = DirtyRates()
    business_timezone: Annotated[str, AfterValidator(check_zone)] = "UTC"


def _fail(key: str, reason: str) -> NoReturn:
    msg = f"{key}: {reason}"
    raise SynthUsageError(msg, key=key)


def _check_arguments(
    scale: str, start: date, end: date, sources: Sequence[str], fetch_mode: str
) -> tuple[ScalePreset, date, tuple[str, ...]]:
    preset = SCALE_PRESETS.get("full" if scale == "5m" else scale)
    if preset is None:
        _fail("scale", "must be one of tiny, small, full, 5m")
    if fetch_mode == "daily" and preset.name == "full":
        _fail("fetch_mode", "daily is allowed only at tiny and small (design §5.1.7)")
    if not sources or not set(sources) <= set(SOURCES):
        _fail("sources", "must be a non-empty subset of servicenow, jira, monitoring, files")
    if preset.span_days is not None:
        start = end - timedelta(days=preset.span_days - 1)
    if start >= end:
        _fail("start", "must be before end")
    return preset, start, tuple(s for s in SOURCES if s in set(sources))


def _read_params_file(path: Path) -> Mapping[str, object]:
    try:
        with path.open("rb") as handle:
            raw = handle.read(PARAMS_FILE_MAX_BYTES + 1)
    except OSError:
        _fail("params_file", "cannot be read")
    if len(raw) > PARAMS_FILE_MAX_BYTES:
        _fail("params_file", f"exceeds {PARAMS_FILE_MAX_BYTES} bytes")
    try:
        loaded = yaml.safe_load(raw.decode("utf-8"))
    except (UnicodeDecodeError, yaml.YAMLError, RecursionError):
        _fail("params_file", "is not valid UTF-8 YAML")
    if not isinstance(loaded, Mapping | None):
        _fail("params_file", "must hold a mapping")
    try:
        tree = _normalize_map(loaded or {}, (), [0])
    except RecursionError:  # self-referential alias such as `a: &a [*a]`
        _fail("params_file", "is too deeply nested or self-referential")
    for key in set(tree) & set(_ARGUMENT_KEYS):
        _fail(key, "is set by a command-line argument, not the params file")
    return tree


def _normalize_map(
    value: Mapping[object, object], path: tuple[str, ...], seen: list[int]
) -> dict[str, object]:
    """Copy a mapping with string keys; keys that collide after conversion are rejected."""
    out: dict[str, object] = {}
    for raw_key, item in value.items():
        key = str(raw_key)
        if key in out:
            _fail(".".join((*path, key)), "is given more than once")
        out[key] = _normalize(item, (*path, key), seen)
    return out


def _normalize(value: object, path: tuple[str, ...], seen: list[int]) -> object:
    """Copy the loaded tree with string keys, counting nodes against `_MAX_NODES`."""
    seen[0] += 1
    if seen[0] > _MAX_NODES or len(path) > _MAX_DEPTH:
        _fail("params_file", "has too many nodes or is nested too deeply")
    if isinstance(value, Mapping):
        return _normalize_map(value, path, seen)
    if isinstance(value, list):
        return [_normalize(v, (*path, str(i)), seen) for i, v in enumerate(value)]
    return value


def _merge(base: Mapping[str, object], override: Mapping[str, object]) -> dict[str, object]:
    """Deep-merge `override` over `base`: maps merge, everything else replaces."""
    merged = dict(base)
    for key, value in override.items():
        current = merged.get(key)
        if isinstance(current, Mapping) and isinstance(value, Mapping):
            merged[key] = _merge(current, value)
        else:
            merged[key] = value
    return merged


def load_params(  # noqa: PLR0913 - signature fixed by U11-02
    scale: str,
    *,
    start: date,
    end: date,
    sources: tuple[str, ...],
    dirty: Literal["none", "default", "heavy"],
    fetch_mode: Literal["initial", "daily"],
    params_file: Path | None,
) -> SynthParams:
    """Validate the arguments, merge `params_file` over the defaults and build the model."""
    preset, start, ordered = _check_arguments(scale, start, end, sources, fetch_mode)
    args: dict[str, Any] = {"scale": preset.name, "preset": preset, "start": start, "end": end}
    args |= {"sources": ordered, "dirty": dirty, "fetch_mode": fetch_mode}
    try:
        base = SynthParams.model_validate(args)
        if params_file is None:
            return base
        groups = base.model_dump(mode="json", exclude=set(_ARGUMENT_KEYS))
        return SynthParams.model_validate(_merge(groups, _read_params_file(params_file)) | args)
    except ValidationError as exc:
        loc = [str(part) for part in exc.errors(include_url=False)[0]["loc"]] or ["params_file"]
        key = loc[0] if loc[0] in _ARGUMENT_KEYS else ".".join(loc)
        _fail(key, exc.errors(include_url=False)[0]["msg"])


def _canonical(value: object) -> str:
    """Canonical JSON: sorted keys, no whitespace, floats as `format(x, ".12g")`."""
    if isinstance(value, dict):
        items = sorted((str(k), v) for k, v in value.items())
        return "{" + ",".join(f"{json.dumps(k)}:{_canonical(v)}" for k, v in items) + "}"
    if isinstance(value, list | tuple):
        return "[" + ",".join(_canonical(v) for v in value) + "]"
    if isinstance(value, float):
        return format(value, ".12g")
    return json.dumps(value)


def params_hash(p: SynthParams) -> str:
    """Return `"sha256:"` + SHA-256 hex of the canonical JSON of the effective parameters."""
    text = _canonical(p.model_dump(mode="json"))
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()
