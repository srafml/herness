"""Truth manifest model and loader (design §4.4, R-64; U11-28, U11-29).

This is the only module under `herness/` allowed to name truth files (TH11-02):
nothing else under `herness/` may open `truth.json`, `truth_labels.parquet` or any
path under `<root>/truth/`. `TruthManifest` is the typed contract shared by the
synthetic-data generator (writer, T11-13) and the evaluation harness (reader, this
card and later).

`SuiteError` is declared here rather than in its eventual home `herness/eval/golden.py`
(module map row, impl spec §3 header): this card (T11-04) is the first unit in the
build order of spec 11 that needs a question-level suite-defect error, and golden.py's
card (which depends on the whole generator pipeline) has not landed yet. When that
module is built it should import `SuiteError` from here instead of redeclaring it, to
keep a single class.
"""

from __future__ import annotations

import datetime
import json
import re
from pathlib import Path
from typing import Annotated, Final, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, ValidationError

from herness.core.errors import ConfigError, RecoverableError

MAX_TRUTH_BYTES: Final = 1_000_000  # design Table F: truth.json cap (U11-28)

_PLANT_PATH_RE: Final = re.compile(r"^(plants\.[A-Za-z0-9_]+|T[0-9]c?)(\.[a-z_]+)+$")


class SuiteError(RecoverableError):
    """A question-level golden-suite defect (U11-29, U11-49)."""


def _parse_date(value: object) -> object:
    return datetime.date.fromisoformat(value) if isinstance(value, str) else value


def _parse_datetime(value: object) -> object:
    return datetime.datetime.fromisoformat(value) if isinstance(value, str) else value


IsoDate = Annotated[datetime.date, BeforeValidator(_parse_date)]
IsoDateTime = Annotated[datetime.datetime, BeforeValidator(_parse_datetime)]


class _Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class T1BadTeam(_Frozen):
    team_id: str
    team_name: str
    service_ids: list[str]
    metric: str
    lever_usd_model: str
    multiplier_start: float
    multiplier_end: float


class T2RoiEpic(_Frozen):
    epic_key: str
    epic_record_id: str
    service_id: str
    cluster_members_file: str
    decoy_epic_key: str


class T2cClusterFix(_Frozen):
    service_id: str
    cluster_members_file: str
    template_id: str


class T3ChangeCluster(_Frozen):
    ci_id: str
    owning_team_id: str
    pairs_file: str
    source_field_share: float


class T4NoisyService(_Frozen):
    service_id: str
    generated_noise_ratio: float


class T5Confounder(_Frozen):
    team_id: str
    service_id: str
    peak_window: list[str]
    volume_multiplier: float


class T6Side(_Frozen):
    epic_key: str
    epic_record_id: str
    service_id: str
    metric: str
    effect: float


class T6Outcomes(_Frozen):
    paid: T6Side
    unpaid: T6Side
    effective_at: IsoDateTime


class Plants(_Frozen):
    T1_bad_team: T1BadTeam
    T2_roi_epic: T2RoiEpic
    T2c_cluster_fix: T2cClusterFix
    T3_change_cluster: T3ChangeCluster
    T4_noisy_service: T4NoisyService
    T5_confounder: T5Confounder
    T6_outcomes: T6Outcomes


class TruthManifest(_Frozen):
    seed: int
    scale: Literal["tiny", "small", "full"]
    generator_version: str
    params_hash: str
    start: IsoDate
    end: IsoDate
    question_set_version: str
    dataset_root: str
    row_counts: dict[str, int]
    dirty: dict[str, int]
    plants: Plants


def truth_dir_for(data_root: Path) -> Path:
    """The truth directory for a build whose pipeline data root is `data_root`."""
    return data_root.parent / "truth"


def load_truth(truth_dir: Path) -> TruthManifest:
    """Read, size-check, parse and validate `<truth_dir>/truth.json` (<= 1 MB)."""
    path = truth_dir / "truth.json"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        msg = "cannot read truth manifest"
        raise ConfigError(msg, hint=str(exc), path=str(path)) from exc
    if len(raw) > MAX_TRUTH_BYTES:
        msg = "truth manifest exceeds 1 MB"
        raise ConfigError(msg, path=str(path))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        msg = "truth manifest is not valid JSON"
        raise ConfigError(msg, hint=str(exc), path=str(path)) from exc
    try:
        return TruthManifest.model_validate(data)
    except ValidationError as exc:
        msg = "truth manifest failed validation"
        raise ConfigError(msg, hint=str(exc), path=str(path)) from exc


def _resolve_short_key(token: str) -> str:
    """Map a short prefix (`T2`, `T2c`, `T6`, ...) to its unique `Plants` field name."""
    prefix = f"{token}_"
    matches = [name for name in Plants.model_fields if name.startswith(prefix)]
    if len(matches) == 1:
        return matches[0]
    msg = "ambiguous plant prefix" if matches else "unknown plant"
    raise SuiteError(msg, path=token)


def plant_value(manifest: TruthManifest, path: str) -> str | float | list[str]:
    """Resolve a truth path (`plants.T1_bad_team.team_id`, short form `T6.paid.epic_key`)."""
    if not _PLANT_PATH_RE.fullmatch(path):
        msg = "malformed truth path"
        raise SuiteError(msg, path=path)
    parts = path.split(".")
    if parts[0] == "plants":
        key, fields = parts[1], parts[2:]
        if key not in Plants.model_fields:
            msg = "unknown plant"
            raise SuiteError(msg, path=path)
    else:
        key, fields = _resolve_short_key(parts[0]), parts[1:]
    value: object = getattr(manifest.plants, key)
    for field in fields:
        if not hasattr(value, field):
            msg = "unknown plant field"
            raise SuiteError(msg, path=path)
        value = getattr(value, field)
    if isinstance(value, list):
        return [str(item) for item in value]
    if isinstance(value, str | float):
        return value
    msg = "truth path does not resolve to a scalar"
    raise SuiteError(msg, path=path)
