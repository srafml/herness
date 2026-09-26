"""Integration test for herness.store.ops.migrate over migrations 001-006 (impl 02 U02-45;
IT02-01)."""

from __future__ import annotations

import importlib
import re
from importlib import resources
from pathlib import Path

import pytest

from herness.store.ops import core
from herness.store.ops.migrate import migrate, schema_version

pytestmark = pytest.mark.integration

_MOD = importlib.import_module("herness.store.ops.migrate")


def _dump() -> list[tuple[str, str, str, str]]:
    """Normalised `sqlite_schema`: (type, name, tbl_name, sql with whitespace collapsed)."""
    rows = core.read_all("SELECT type, name, tbl_name, sql FROM sqlite_schema")
    return sorted(
        (
            str(r["type"]),
            str(r["name"]),
            str(r["tbl_name"]),
            re.sub(r"\s+", " ", str(r["sql"] or "")).strip(),
        )
        for r in rows
    )


def _copy(target: Path, names: list[str]) -> Path:
    target.mkdir()
    root = resources.files("herness.store.migrations")
    for name in names:
        (target / name).write_bytes(root.joinpath(name).read_bytes())
    return target


@pytest.mark.parametrize("k", [1, 2, 3, 4, 5])
def test_it02_01_stepwise_apply_equals_fresh(
    k: int, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT02-01 applying 1…k, reconnecting and applying the rest equals a fresh migration."""
    root = resources.files("herness.store.migrations")
    names = sorted(
        e.name for e in root.iterdir() if re.fullmatch(r"00[1-6]_[a-z0-9_]+\.sql", e.name)
    )
    assert len(names) == 6
    partial = _copy(tmp_path / "partial", names[:k])
    full = _copy(tmp_path / "full", names)

    core.reset_connections(path=tmp_path / "fresh.sqlite")
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
    try:
        migrate()
        fresh = _dump()

        core.reset_connections(path=tmp_path / "stepwise.sqlite")
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: partial)
        assert len(migrate().applied) == k
        assert schema_version() == k
        core.reset_connections(path=tmp_path / "stepwise.sqlite")  # reconnect
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
        assert len(migrate().applied) == 6 - k
        assert schema_version() == 6
        stepwise = _dump()
    finally:
        core.reset_connections()
    assert stepwise == fresh
    assert any(row[1] == "memory_fts" for row in fresh)
