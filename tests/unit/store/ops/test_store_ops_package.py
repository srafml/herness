"""Unit tests for the herness.store.ops namespace (impl 02 U02-62; UT02-68).

Blocks "02 core" (T02-04), "02 migrate" (T02-05) and "02 shared" exist. T02-07 added the
U02-55 … U02-60 names of "02 shared"; T02-24 adds U02-130 … U02-132, so the shared test checks
the T02-07 names and that the block is a U02-62-ordered subset.
"""

from __future__ import annotations

import importlib
import re
import sqlite3
import sys
from pathlib import Path

import pytest

from herness.store import ops
from herness.store.ops import core

pytestmark = pytest.mark.unit

# Canonical area table of impl 02 §2.3.
AREAS = (
    "core",
    "migrate",
    "shared",
    "ingest",
    "evidence",
    "runs",
    "findings",
    "memory",
    "closed_loop",
    "jobs",
    "tasks",
    "worker",
    "resilience",
    "metrics",
    "chat",
    "ui_reads",
    "privacy",
)
_HEADER = re.compile(r"^\s*#\s*(\d{2})\s+([a-z_]+)\s*$")
_NAME = re.compile(r'^\s*"([A-Za-z_][A-Za-z0-9_]*)",?\s*$')
_CORE_BLOCK = (
    "connection",
    "run_write",
    "read_one",
    "read_all",
    "dump_json",
    "load_json",
    "reset_connections",
    "OPS_JSON_MAX_BYTES",
)
_MIGRATE_BLOCK = (
    "MIGRATION_RANGES",
    "migrate",
    "pending_migrations",
    "schema_version",
    "ops_health",
    "MigrationReport",
)

_SHARED_BLOCK = (
    "ReviewItem",
    "ReviewKind",
    "ReviewStatus",
    "create_review_item",
    "create_review_item_if_absent",
    "get_review_item",
    "list_review_items",
    "count_review_items",
    "decide_review_item",
    "update_review_payload",
    "approved_mapping_suggestions",
)


# The U02-55 … U02-60 names (T02-07); T02-24 adds the other three of _SHARED_BLOCK.
_SHARED_T02_07 = (
    "ReviewItem",
    "ReviewKind",
    "ReviewStatus",
    "create_review_item",
    "get_review_item",
    "list_review_items",
    "decide_review_item",
    "approved_mapping_suggestions",
)


def _blocks() -> list[tuple[str, str, list[str]]]:
    """Parse `__all__` of the package source into (owner spec, area, names) blocks."""
    source = Path(ops.__file__).read_text(encoding="utf-8").splitlines()
    start = next(i for i, line in enumerate(source) if line.startswith("__all__"))
    blocks: list[tuple[str, str, list[str]]] = []
    for line in source[start + 1 :]:
        if line.startswith("]"):
            break
        header = _HEADER.match(line)
        if header is not None:
            blocks.append((header[1], header[2], []))
            continue
        name = _NAME.match(line)
        if name is not None:
            assert blocks, "a name precedes the first block header"
            blocks[-1][2].append(name[1])
    return blocks


def test_ut02_68_all_has_no_duplicates() -> None:
    """UT02-68 `__all__` has no duplicate names and equals the parsed blocks."""
    names = [name for _, _, block in _blocks() for name in block]
    assert len(names) == len(set(names))
    assert list(ops.__all__) == names


def test_ut02_68_core_block_first_and_complete() -> None:
    """UT02-68 block "02 core" comes first and lists the U02-62 names in order."""
    blocks = _blocks()
    assert blocks[0] == ("02", "core", list(_CORE_BLOCK))


def test_ut02_68_migrate_block_second_and_complete() -> None:
    """UT02-68 block "02 migrate" follows "02 core" and lists the U02-62 names in order."""
    assert _blocks()[1] == ("02", "migrate", list(_MIGRATE_BLOCK))


def test_ut02_68_shared_block_third_in_u02_62_order() -> None:
    """UT02-68 block "02 shared" is third and a U02-62-ordered subset holding T02-07's names."""
    blocks = _blocks()
    shared = [i for i, (_, area, _) in enumerate(blocks) if area == "shared"]
    assert shared == [2]
    owner, _, names = blocks[2]
    assert owner == "02"
    # Only U02-62 names, in U02-62 order (T02-07 lands 8 of them, T02-24 the other 3).
    assert names == [n for n in _SHARED_BLOCK if n in names]
    assert set(_SHARED_T02_07) <= set(names)


def test_ut02_68_names_resolve_to_their_area() -> None:
    """UT02-68 every name resolves to the attribute of the area named in its block header."""
    for _, area, names in _blocks():
        assert area in AREAS
        module = importlib.import_module(f"herness.store.ops.{area}")
        for name in names:
            assert getattr(ops, name) is getattr(module, name), name
    assert ops.run_write is core.run_write


def test_ut02_68_no_name_equals_an_area() -> None:
    """UT02-68 only the function `migrate` shares an area name; the area stays importable."""
    assert set(ops.__all__) & set(AREAS) == {"migrate"}
    area = sys.modules["herness.store.ops.migrate"]
    assert callable(ops.migrate)
    assert ops.migrate is area.migrate
    from herness.store.ops.migrate import pending_migrations  # noqa: PLC0415 - the check itself

    assert pending_migrations is ops.pending_migrations is area.pending_migrations


def test_ut02_68_review_item_only_in_shared() -> None:
    """UT02-68 no `review_item` function outside block "shared" (R-08)."""
    for _, area, names in _blocks():
        if area != "shared":
            assert not [n for n in names if "review_item" in n], area


def test_ut02_68_import_opens_no_connection(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-68 importing the package opens no connection."""
    core.reset_connections()
    calls: list[object] = []

    def no_connect(*args: object, **kwargs: object) -> None:
        calls.append(args)
        msg = "connect during import"
        raise AssertionError(msg)

    monkeypatch.setattr(sqlite3, "connect", no_connect)
    # Core first, so the package re-binds the reloaded functions (keeps `is` identities).
    importlib.reload(core)
    importlib.reload(ops)
    assert calls == []
    assert ops.run_write is core.run_write
    assert core._registry.connections == []
