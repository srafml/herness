"""tests.support.ops_store: fixture `ops_store`, `OpsStoreHandle` (U11-78).

Gives each test a fresh, fully migrated `ops.sqlite` under `tmp_path`, with the loaded
config's `paths.data` pointed at the same `tmp_path/data` directory. This is the real
spec 11 fixture; it replaces the interim `tests.support.ops_core_store` that T02-04 added
before T02-05's migration runner and T10-03's config reset existed (T11-40).

Merge-compat: `OpsStoreHandle` implements `os.PathLike` (`__fspath__`) and forwards unknown
attribute lookups to `db_path`, so code written against the interim `ops_store: Path` API
by a parallel work group keeps working unchanged regardless of merge order, e.g.
`ops_store.is_file()` or `open(ops_store)`.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from herness.core.config import reset_config
from herness.store.ops import reset_connections
from herness.store.ops.migrate import MigrationReport, migrate


@dataclass(frozen=True, slots=True)
class OpsStoreHandle:
    """A fresh, migrated ops store (U11-78): its data root, db file and migration report."""

    data_root: Path
    db_path: Path
    migration: MigrationReport

    def __fspath__(self) -> str:
        return str(self.db_path)

    def __truediv__(self, other: str | os.PathLike[str]) -> Path:
        return self.db_path / other

    def __getattr__(self, name: str) -> Any:
        if name in {"data_root", "db_path", "migration"} or name.startswith("__"):
            raise AttributeError(name)  # uninitialised handle or protocol probe: no recursion
        return getattr(self.db_path, name)


@pytest.fixture
def ops_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[OpsStoreHandle]:
    """Fresh migrated ops store under `tmp_path`; config `paths.data` points at it (U11-78)."""
    data_root = tmp_path / "data"
    data_root.mkdir()
    monkeypatch.setenv("HERNESS_PATHS__DATA", str(data_root))
    reset_config()
    db_path = data_root / "ops.sqlite"
    reset_connections(path=db_path)
    try:
        report = migrate()
        yield OpsStoreHandle(data_root, db_path, report)
    finally:
        reset_connections()
        reset_config()
