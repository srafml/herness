"""Resolve the data root into the paths of every store (impl 02 U02-06, U02-07)."""

from __future__ import annotations

import dataclasses
import itertools
from pathlib import Path

from herness.core.config import HernessConfig, get_config


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


def data_layout(*, cfg: HernessConfig | None = None, root: Path | None = None) -> DataLayout:
    """Turn ``root``, else ``cfg.paths.data`` (default: ``get_config()``), into a layout."""
    if root is None:
        root = (cfg if cfg is not None else get_config()).paths.data  # ConfigError propagates
    return DataLayout.from_root(Path(root))
