"""Inventory of lake entities and their columns (impl 02 U02-78 … U02-81).

Staging SQL tolerates missing entities and missing columns (schema drift, design 02 §11):
the build renders each raw column through ``raw()``, which consults this inventory.
Only committed files count; dot-prefixed temp files are excluded exactly as the staging
glob excludes them (``LAKE_FILE_PATTERN``).
"""

from __future__ import annotations

import dataclasses
import itertools
import types
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Final

import pyarrow.parquet as pq

from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.store.lake import LAKE_FILE_PATTERN, lake_glob
from herness.store.layout import DataLayout

EXPECTED_ENTITIES: Final[tuple[tuple[str, str], ...]] = (
    ("servicenow", "incident"),
    ("servicenow", "change_request"),
    ("servicenow", "problem"),
    ("servicenow", "cmdb_ci"),
    ("servicenow", "cmdb_ci_service"),
    ("servicenow", "cmdb_rel_ci"),
    ("servicenow", "sys_user_group"),
    ("servicenow", "cmn_department"),
    ("servicenow", "task_sla"),
    ("jira", "issue"),
    ("monitoring", "event"),
    ("monitoring", "metric_daily"),
)

MAX_FILES_PER_ENTITY: Final = 100_000

_log = get_logger("model.build")


@dataclasses.dataclass(frozen=True, slots=True)
class EntityInventory:
    """What the lake holds for one entity; ``present`` iff ``files > 0``."""

    source: str
    entity: str
    glob: str
    present: bool
    files: int
    bytes: int
    columns: frozenset[str]
    from_synth: bool

    def __post_init__(self) -> None:
        if self.present != (self.files > 0):
            msg = "EntityInventory.present must equal files > 0"
            raise ValueError(msg)


@dataclasses.dataclass(frozen=True, slots=True)
class LakeInventory:
    """Inventory of the raw root for the render context and ``dataset_kind``."""

    root: Path
    entities: Mapping[tuple[str, str], EntityInventory]
    from_synth: bool = False

    def __post_init__(self) -> None:
        frozen = types.MappingProxyType(dict(self.entities))
        object.__setattr__(self, "entities", frozen)

    def get(self, source: str, entity: str) -> EntityInventory:
        """Return the entity's inventory; an unknown entity is absent with no columns."""
        found = self.entities.get((source, entity))
        if found is not None:
            return found
        return _absent(self.root, source, entity, from_synth=False)


def _absent(raw: Path, source: str, entity: str, *, from_synth: bool) -> EntityInventory:
    return EntityInventory(
        source=source,
        entity=entity,
        glob=lake_glob(raw, source, entity),
        present=False,
        files=0,
        bytes=0,
        columns=frozenset(),
        from_synth=from_synth,
    )


def _has_synth_pair(path: Path) -> bool:
    lowered = [part.lower() for part in path.parts]
    return any(a == "data" and b == "synth" for a, b in itertools.pairwise(lowered))


def _unique(pairs: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    return list(dict.fromkeys(pairs))


def _committed_files(raw: Path, source: str, entity: str) -> list[Path]:
    entity_dir = raw / source / entity
    if not entity_dir.is_dir():
        return []
    matches = (p for p in entity_dir.glob("**/" + LAKE_FILE_PATTERN) if p.is_file())
    files = list(itertools.islice(matches, MAX_FILES_PER_ENTITY + 1))
    if len(files) > MAX_FILES_PER_ENTITY:
        msg = f"too many lake files for {source}/{entity}"
        raise SchemaViolation(msg)
    return sorted(files)


def _read_names(raw: Path, path: Path) -> list[str]:
    try:
        return list(pq.read_schema(path).names)
    except (OSError, ValueError) as exc:  # ArrowInvalid is a ValueError subclass
        rel = path.relative_to(raw).as_posix()
        msg = f"unreadable lake file {rel}"
        raise SchemaViolation(msg) from exc


def _scan_entity(layout: DataLayout, source: str, entity: str) -> EntityInventory:
    raw = layout.raw
    glob = lake_glob(raw, source, entity)  # validates both names before touching the disk
    files = _committed_files(raw, source, entity)
    if not files:
        return _absent(raw, source, entity, from_synth=layout.synth_marker)
    columns: set[str] = set()
    size = 0
    synth = layout.synth_marker
    for path in files:
        columns.update(_read_names(raw, path))
        size += path.stat().st_size
        synth = synth or _has_synth_pair(path.resolve())
    return EntityInventory(
        source=source,
        entity=entity,
        glob=glob,
        present=True,
        files=len(files),
        bytes=size,
        columns=frozenset(columns),
        from_synth=synth,
    )


def scan_lake(
    layout: DataLayout, *, extra_entities: Sequence[tuple[str, str]] = ()
) -> LakeInventory:
    """Scan every expected and configured entity: committed files, bytes, column union.

    Raises SchemaViolation for more than ``MAX_FILES_PER_ENTITY`` files in one entity or an
    unreadable file, and ConfigError for an invalid source or entity name.
    """
    entities: dict[tuple[str, str], EntityInventory] = {}
    for source, entity in _unique((*EXPECTED_ENTITIES, *extra_entities)):
        entities[(source, entity)] = _scan_entity(layout, source, entity)
    from_synth = layout.synth_marker or any(e.from_synth for e in entities.values())
    present = [e for e in entities.values() if e.present]
    _log.info(
        "model.build.lake_scanned",
        entities_present=len(present),
        files=sum(e.files for e in present),
        bytes=sum(e.bytes for e in present),
    )
    return LakeInventory(root=layout.raw, entities=entities, from_synth=from_synth)
