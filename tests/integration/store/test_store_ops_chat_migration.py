"""Integration test for migration 090_chat (impl 09 U09-43; IT09-23).

A fresh store migrated through 090 and a store at the previous migration (006) upgraded to 090
end with the same schema; the reply index and the summary column exist; 090 creates no table.
"""

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
_CHAT_SQL = "090_chat.sql"


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


def _through_090() -> list[str]:
    root = resources.files("herness.store.migrations")
    names = sorted(e.name for e in root.iterdir() if re.fullmatch(r"\d{3}_[a-z0-9_]+\.sql", e.name))
    return [n for n in names if int(n[:3]) <= 90]


def _column_names() -> set[str]:
    return {str(r["name"]) for r in core.read_all("PRAGMA table_info(chat_session)")}


def _index_names() -> set[str]:
    return {str(r["name"]) for r in core.read_all("PRAGMA index_list(chat_message)")}


def test_it09_23_upgrade_from_previous_equals_fresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT09-23 a DB at the previous migration upgraded to 090 has the fresh DB's schema."""
    names = _through_090()
    assert names[-1] == _CHAT_SQL
    full = _copy(tmp_path / "full", names)
    previous = _copy(tmp_path / "previous", names[:-1])
    try:
        core.reset_connections(path=tmp_path / "fresh.sqlite")
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
        migrate()
        fresh = _dump()
        assert "chat_message_reply" in _index_names()

        core.reset_connections(path=tmp_path / "upgraded.sqlite")
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: previous)
        migrate()
        assert "chat_message_reply" not in _index_names()
        assert "summary_through_message_id" not in _column_names()
        core.reset_connections(path=tmp_path / "upgraded.sqlite")  # reconnect, as a restart
        monkeypatch.setattr(_MOD, "_migrations_root", lambda: full)
        assert migrate().applied == ("090_chat",)
        assert schema_version() == 90
        upgraded = _dump()
    finally:
        core.reset_connections()
    assert upgraded == fresh


def test_it09_23_index_and_column_present(ops_store: Path) -> None:
    """IT09-23 PRAGMA index_list shows chat_message_reply; table_info shows the new column."""
    migrate()
    indexes = {str(r["name"]): r for r in core.read_all("PRAGMA index_list(chat_message)")}
    assert {"chat_message_reply", "chat_message_session"} <= set(indexes)
    reply = indexes["chat_message_reply"]
    assert (reply["unique"], reply["partial"]) == (1, 1)
    info = {str(r["name"]): r for r in core.read_all("PRAGMA table_info(chat_session)")}
    column = info["summary_through_message_id"]
    assert (column["type"], column["notnull"], column["dflt_value"]) == ("TEXT", 0, None)


def test_it09_23_migration_creates_no_table() -> None:
    """IT09-23 090_chat.sql contains no CREATE TABLE and stays within its 20-line budget."""
    text = resources.files("herness.store.migrations").joinpath(_CHAT_SQL).read_text("utf-8")
    assert re.search(r"CREATE\s+TABLE", text, re.IGNORECASE) is None
    assert "CREATE UNIQUE INDEX IF NOT EXISTS chat_message_reply" in text
    assert "ALTER TABLE chat_session ADD COLUMN summary_through_message_id TEXT" in text
    assert len(text.splitlines()) <= 20
