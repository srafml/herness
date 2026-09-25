"""Resolve the data root into the paths of every store (impl 02 U02-06, U02-07)."""

from __future__ import annotations

import dataclasses
import importlib
import itertools
from pathlib import Path
from typing import Protocol

_CONFIG_MODULE = "herness.core.config"


class _PathsSection(Protocol):
    @property
    def data(self) -> Path | str: ...


class _ConfigWithPaths(Protocol):
    """Structural view of ``T10-03 (herness.core.config.HernessConfig)``: ``paths.data``."""

    @property
    def paths(self) -> _PathsSection: ...


def _has_synth_pair(parts: tuple[str, ...]) -> bool:
    lowered = [part.lower() for part in parts]
    return any(a == "data" and b == "synth" for a, b in itertools.pairwise(lowered))


@dataclasses.dataclass(frozen=True, slots=True)
class DataLayout:
    """Absolute paths of every store under the data root; creates nothing."""

    root: Path
    raw: Path
    warehouse: Path
    ops_db: Path
    vectors: Path
    synth_marker: bool

    @classmethod
    def from_root(cls, root: Path) -> DataLayout:
        """Resolve ``root`` once (TH02-01) and derive the child paths."""
        resolved = root.resolve(strict=False)
        return cls(
            root=resolved,
            raw=resolved / "raw",
            warehouse=resolved / "warehouse",
            ops_db=resolved / "ops.sqlite",
            vectors=resolved / "vectors",
            synth_marker=_has_synth_pair(resolved.parts),
        )


def _loaded_config() -> _ConfigWithPaths:
    # The config loader (impl 10, T10-03) is looked up at call time; ConfigError propagates.
    module = importlib.import_module(_CONFIG_MODULE)
    config: _ConfigWithPaths = module.get_config()
    return config


def data_layout(*, cfg: _ConfigWithPaths | None = None, root: Path | None = None) -> DataLayout:
    """Turn ``root``, else ``cfg.paths.data`` (default: the loaded config), into a layout."""
    if root is None:
        source = cfg if cfg is not None else _loaded_config()
        root = Path(source.paths.data)
    return DataLayout.from_root(Path(root))
