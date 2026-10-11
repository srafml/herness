"""IT07-08 (U07-20): a store at the version before 070, holding items written by the real spec
07 `MemoryStore`, upgraded to the head migration (T07-24).

Unlike the T07-03 row in `tests/integration/store/test_store_ops_memory_migration.py` (raw SQL
items, upgrade through 070 only), the fixture DB here is filled through the facade's own write
path (`propose`, which writes `memory_item`, its FTS row through the 004 triggers and the
vector) and is then upgraded by the real runner in two restarts: first to 070, then to the
head; recall over the upgraded store runs through the facade.
"""

from __future__ import annotations

import importlib
import re
from importlib import resources
from pathlib import Path

import pytest
from tests.integration.memory._it07_env import memory_store
from tests.unit.harness.memory._write_env import proposal

from herness.store.ops import core, memory
from herness.store.ops.migrate import migrate, schema_version

pytestmark = pytest.mark.integration

_MOD = importlib.import_module("herness.store.ops.migrate")
_INDEXES_070 = {
    "ix_memory_expires", "ix_memory_content_hash", "ix_memory_task", "ix_memory_fingerprint",
    "ix_memory_rec", "ix_memory_prov_run", "ix_memory_prov_session", "ix_memory_prov_author",
    "ix_rec_target", "ix_rec_metric",
}  # fmt: skip
_TEXTS = ("Churn means customers who cancelled.", "MTTR means mean time to restore.")

type Schema = set[tuple[str, str, str, str]]


def _dump() -> Schema:
    rows = core.read_all("SELECT type, name, tbl_name, sql FROM sqlite_schema")
    return {(str(r["type"]), str(r["name"]), str(r["tbl_name"]),
             re.sub(r"\s+", " ", str(r["sql"] or "")).strip()) for r in rows}  # fmt: skip


def _names() -> list[str]:
    root = resources.files("herness.store.migrations")
    return sorted(e.name for e in root.iterdir() if re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", e.name))


def _copy(target: Path, names: list[str]) -> Path:
    target.mkdir()
    root = resources.files("herness.store.migrations")
    for name in names:
        (target / name).write_bytes(root.joinpath(name).read_bytes())
    return target


def _migrate_with(monkeypatch: pytest.MonkeyPatch, root: Path, db: Path) -> tuple[str, ...]:
    core.reset_connections(path=db)  # a restart: a fresh connection on the same file
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: root)
    return migrate().applied


def _fts_ids(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ?", (term,),
    )  # fmt: skip
    return [str(r[0]) for r in rows]


def test_it07_08_facade_written_store_upgraded_to_head_equals_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-08 a fixture DB at the version before 070 with facade-written items: the 070 step
    adds only the ten indexes (R-11), the upgrade to the head equals a fresh head schema, FTS
    is in sync and the facade recalls both items from the upgraded store."""
    names = _names()
    i070 = names.index("070_memory_indexes.sql")
    head = _copy(tmp_path / "head", names)
    pre = _copy(tmp_path / "pre070", names[:i070])
    upto = _copy(tmp_path / "upto070", names[: i070 + 1])
    data_root = tmp_path / "data"
    data_root.mkdir()
    db = data_root / "ops.sqlite"
    try:
        _migrate_with(monkeypatch, head, tmp_path / "fresh.sqlite")
        fresh = _dump()
        assert _migrate_with(monkeypatch, pre, db)[-1] == names[i070 - 1][:-4]
        mem = memory_store(tmp_path, data_root)
        ids = [mem.store.propose(proposal(text)).memory_id for text in _TEXTS]
        before = _dump()
        assert _migrate_with(monkeypatch, upto, db) == ("070_memory_indexes",)
        after_070 = _dump()
        rest = tuple(n[:-4] for n in names[i070 + 1 :])
        assert _migrate_with(monkeypatch, head, db) == rest
        assert schema_version() == int(names[-1][:3])
        upgraded = _dump()
        assert memory.fts_check_and_rebuild() is False  # index matches the content table
        assert _fts_ids("churn") == [ids[0]]
        assert _fts_ids("restore") == [ids[1]]
        hits = [
            {h.item.memory_id for h in mem.store.recall(f"glossary: {text}", k=5)}
            for text in _TEXTS
        ]  # the item embedding text (U07-58): ANN similarity 1
        rows = {r["memory_id"]: r for r in memory.get_memory_items(ids)}
    finally:
        core.reset_connections()
    assert upgraded == fresh
    assert before <= after_070  # nothing removed or changed by 070
    added = after_070 - before
    assert {kind for kind, *_ in added} == {"index"}
    assert {name for _, name, *_ in added} == _INDEXES_070
    assert [memory_id in found for memory_id, found in zip(ids, hits, strict=True)] == [True, True]
    assert [rows[i]["content"] for i in ids] == list(_TEXTS)
