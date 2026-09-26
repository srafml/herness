"""Integration test for migration 070_memory_indexes (impl 07 U07-20; IT07-08).

A store at the migration before 070 (006), holding memory items, upgraded through 070 ends with
the schema of a fresh store migrated through 070; the upgrade adds exactly the ten indexes of
impl 07 §4.1 and nothing else (R-11); ``memory_fts`` stays in sync with ``memory_item``.
"""

from __future__ import annotations

import importlib
import re
from importlib import resources
from pathlib import Path

import pytest

from herness.store.ops import core, memory
from herness.store.ops.migrate import migrate, schema_version

pytestmark = pytest.mark.integration

_MOD = importlib.import_module("herness.store.ops.migrate")
_SQL = "070_memory_indexes.sql"
_INDEXES_070 = {
    "ix_memory_expires",
    "ix_memory_content_hash",
    "ix_memory_task",
    "ix_memory_fingerprint",
    "ix_memory_rec",
    "ix_memory_prov_run",
    "ix_memory_prov_session",
    "ix_memory_prov_author",
    "ix_rec_target",
    "ix_rec_metric",
}
_IDS = ("mem_01J0000000000000000000000A", "mem_01J0000000000000000000000B")
_NOW = "2026-09-26T10:00:00.000000Z"

type Schema = list[tuple[str, str, str, str]]


def _dump() -> Schema:
    """Normalised ``sqlite_schema``: (type, name, tbl_name, sql with whitespace collapsed)."""
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


def _through_070() -> list[str]:
    root = resources.files("herness.store.migrations")
    names = sorted(e.name for e in root.iterdir() if re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", e.name))
    return [n for n in names if int(n[:3]) <= 70]


def _seed() -> None:
    """Two items written with raw SQL, as a pre-070 store would hold them."""
    sql = (
        "INSERT INTO memory_item (memory_id, layer, kind, content, data, provenance, confidence,"
        " status, created_at) VALUES (?, 'semantic', 'glossary', ?, ?, '{}', 0.5, 'active', ?)"
    )
    params = [
        (_IDS[0], "churn means cancelled", '{"content_hash": "' + "a" * 32 + '"}', _NOW),
        (_IDS[1], "revenue grows", "{}", _NOW),
    ]
    core.run_write(lambda c: c.executemany(sql, params), op="test_seed")


def _match(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ?",
        (term,),
    )
    return [str(r[0]) for r in rows]


def test_it07_08_upgrade_from_previous_equals_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-08 a DB before 070 upgraded through 070 has the fresh schema, only new indexes."""
    names = _through_070()
    assert names[-1] == _SQL
    full = _copy(tmp_path / "full", names)
    previous = _copy(tmp_path / "previous", names[:-1])
    try:
        core.reset_connections(path=tmp_path / "fresh.sqlite")
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
        migrate()
        fresh = _dump()

        core.reset_connections(path=tmp_path / "upgraded.sqlite")
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: previous)
        migrate()
        _seed()
        before = _dump()
        core.reset_connections(path=tmp_path / "upgraded.sqlite")  # reconnect, as a restart
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
        assert migrate().applied == ("070_memory_indexes",)
        assert schema_version() == 70
        upgraded = _dump()
        assert memory.fts_check_and_rebuild() is False  # index matches the content table
        assert _match("churn") == [_IDS[0]]
        assert memory.get_memory_items(list(_IDS))[1]["content"] == "revenue grows"
    finally:
        core.reset_connections()
    assert upgraded == fresh
    assert set(before) <= set(upgraded)  # nothing removed or changed
    added = set(upgraded) - set(before)
    assert {kind for kind, *_ in added} == {"index"}
    assert {name for _, name, *_ in added} == _INDEXES_070


def test_it07_08_migration_holds_only_index_statements() -> None:
    """IT07-08 070 holds ten CREATE INDEX IF NOT EXISTS statements, no other DDL or DML."""
    text = resources.files("herness.store.migrations").joinpath(_SQL).read_text("utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.startswith("--"))
    statements = [s.strip() for s in code.split(";") if s.strip()]
    assert len(statements) == len(_INDEXES_070)
    assert all(s.startswith("CREATE INDEX IF NOT EXISTS ") for s in statements)
    assert [s.split()[5] for s in statements] == [
        "ix_memory_expires",
        "ix_memory_content_hash",
        "ix_memory_task",
        "ix_memory_fingerprint",
        "ix_memory_rec",
        "ix_memory_prov_run",
        "ix_memory_prov_session",
        "ix_memory_prov_author",
        "ix_rec_target",
        "ix_rec_metric",
    ]
    assert len(text.splitlines()) <= 40
