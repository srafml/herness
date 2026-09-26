"""Data path resolution and the on-disk layout of impl 03 §4.2-§4.7 (U03-12, U03-13).

Identifiers are validated before joining; configured paths stay in the data root (TH03-08)."""

from __future__ import annotations

import dataclasses
import re
from pathlib import Path
from typing import Literal, Protocol, get_args
from urllib.parse import quote

from pydantic import BaseModel

from herness.core.errors import ConfigError
from herness.core.types import DecisionOutput, QuestionSet
from herness.enrich.settings import DecidersSettings, EmbeddingSettings

LabelKind = Literal["teacher", "human", "gold"]


def _field_pattern(model: type[BaseModel], name: str) -> re.Pattern[str]:
    """The ``Field(pattern=...)`` of ``model.name``: one source for the identifier rules."""
    patterns = [getattr(m, "pattern", None) for m in model.model_fields[name].metadata]
    return re.compile(next(p for p in patterns if isinstance(p, str)))


_SLUG_RE = re.compile(r"^[A-Za-z0-9._-]{1,96}$")
_RULES: dict[str, re.Pattern[str] | frozenset[str]] = {
    "qsv": _field_pattern(QuestionSet, "version"),
    "version": re.compile(r"^laya-\d{8}-\d+$"),
    "decider": frozenset(get_args(DecisionOutput.model_fields["decider"].annotation)),
    "decider_version": _field_pattern(DecisionOutput, "decider_version"),
    "algorithm_version": _SLUG_RE,
    "snapshot_id": _SLUG_RE,
    "kind": frozenset(get_args(LabelKind)),
}
_DOT_NAMES = frozenset({".", ".."})


def resolve_data_path(path: str | Path, /, *, data_root: Path) -> Path:
    """Resolve ``path`` (``data/...``, relative or absolute) to an absolute path under
    ``data_root`` (U03-12). Raises ConfigError when it lands outside the data root."""
    root, candidate = data_root.resolve(), Path(path)
    parts = candidate.parts
    if not candidate.is_absolute() and parts[:1] == ("data",):
        parts = parts[1:]  # an absolute path replaces the root in joinpath
    result = root.joinpath(*parts).resolve()
    if not result.is_relative_to(root):
        msg = f"path outside data root: {path}"
        raise ConfigError(msg)
    return result


def _valid(argument: str, value: str) -> str:
    rule = _RULES[argument]
    ok = value in rule if isinstance(rule, frozenset) else rule.fullmatch(value) is not None
    if not ok or value in _DOT_NAMES:
        msg = f"invalid {argument} for an enrichment path"
        raise ConfigError(msg, argument=argument)
    return value


class _PathsSection(Protocol):
    @property
    def data(self) -> Path | str: ...


class _DecisionsSection(Protocol):
    @property
    def embedding(self) -> EmbeddingSettings: ...


class _ModelsSection(Protocol):
    @property
    def deciders(self) -> DecidersSettings: ...


class _EnrichConfig(Protocol):  # T10-03: retype as herness.core.config.HernessConfig
    @property
    def paths(self) -> _PathsSection: ...
    @property
    def decisions(self) -> _DecisionsSection: ...
    @property
    def models(self) -> _ModelsSection: ...


@dataclasses.dataclass(frozen=True, slots=True)
class EnrichPaths:
    """Single source of every on-disk path of this package (U03-13); touches no disk."""

    data_root: Path
    embedding_path: str
    laya_current_file: str

    def __post_init__(self) -> None:
        if not self.data_root.is_absolute():
            msg = "invalid data_root for an enrichment path: not absolute"
            raise ConfigError(msg, argument="data_root")

    @classmethod
    def from_config(cls, cfg: _EnrichConfig) -> EnrichPaths:
        """Build from ``paths.data``, ``decisions.embedding.path`` and the Laya CURRENT file."""
        return cls(
            data_root=Path(cfg.paths.data).resolve(),
            embedding_path=cfg.decisions.embedding.path,
            laya_current_file=cfg.models.deciders.laya.current_file,
        )

    def cache_dir(self, qsv: str) -> Path:
        return self.data_root / "cache" / "decisions" / _valid("qsv", qsv)

    def cache_partition(self, qsv: str, decider: str, decider_version: str) -> Path:
        encoded = quote(_valid("decider_version", decider_version), safe="")
        partition = f"decider={_valid('decider', decider)}"
        return self.cache_dir(qsv) / partition / f"decider_version={encoded}"

    def labels_dir(self, qsv: str, kind: LabelKind) -> Path:
        return self.data_root / "labels" / _valid("qsv", qsv) / _valid("kind", kind)

    def laya_root(self) -> Path:
        return self.data_root / "models" / "laya"

    def laya_dir(self, version: str) -> Path:
        return self.laya_root() / _valid("version", version)

    def laya_current(self) -> Path:
        return resolve_data_path(self.laya_current_file, data_root=self.data_root)

    def calibration_file(self, decider: str, decider_version: str, qsv: str) -> Path:
        base = self.data_root / "models" / "calibration" / _valid("decider", decider)
        encoded = quote(_valid("decider_version", decider_version), safe="")
        return base / encoded / f"{_valid('qsv', qsv)}.json"

    def cluster_root(self, algorithm_version: str) -> Path:
        clusters = self.data_root / "models" / "clusters"
        return clusters / _valid("algorithm_version", algorithm_version)

    def cluster_snapshot(self, algorithm_version: str, snapshot_id: str) -> Path:
        return self.cluster_root(algorithm_version) / _valid("snapshot_id", snapshot_id)

    def pairs_dir(self) -> Path:
        return self.data_root / "cache" / "pairs"

    def embedding_model_dir(self) -> Path:
        return resolve_data_path(self.embedding_path, data_root=self.data_root)

    def vectors_dir(self) -> Path:
        return self.data_root / "vectors"
