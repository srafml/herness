"""Unit tests for herness.store.ops.evidence (impl 05 U05-71, U05-75; UT05-47, UT05-48,
UT05-126). UT05-47's type parts (Evidence validation) are covered by T05-02; this file adds
the store parts.
"""

from __future__ import annotations

import importlib
from collections.abc import Iterator
from datetime import UTC, datetime
from importlib import resources
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from structlog.testing import capture_logs

from herness.core.errors import SchemaViolation
from herness.core.ids import new_ulid
from herness.core.ids import query_id as compute_query_id
from herness.core.types.harness.evidence import Evidence
from herness.store.ops import core
from herness.store.ops.evidence import (
    finding_statuses,
    get_evidence,
    record_evidence,
    record_evidence_use,
    scrub_record_from_evidence,
)
from herness.store.ops.migrate import migrate

pytestmark = pytest.mark.unit

_MOD: ModuleType = importlib.import_module("herness.store.ops.migrate")
_BUILD = "20260101-000000-ABCDEF"
_TS = "2026-09-26T10:00:00.000000Z"


def _package_sql() -> dict[str, bytes]:
    root = resources.files("herness.store.migrations")
    return {e.name: e.read_bytes() for e in root.iterdir() if e.name.endswith(".sql")}


@pytest.fixture
def ops_store(tmp_path: Path) -> Iterator[Path]:
    """Local override: the stepwise variant upgrades 001-002 then 003+ itself, so it needs an
    empty, un-migrated store, not the plugin `ops_store` (which is pre-migrated, T11-40)."""
    db_path = tmp_path / "ops.sqlite"
    core.reset_connections(path=db_path)
    yield db_path
    core.reset_connections()


@pytest.fixture(params=["fresh", "stepwise"])
def migrated(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    ops_store: Path,
) -> Path:
    """A fresh-migrated store, or one upgraded stepwise (001-002, then 003-006 to latest)."""
    if request.param == "fresh":
        migrate()
        return ops_store
    target = tmp_path / "migrations"
    target.mkdir()
    sql = _package_sql()
    early = {name: data for name, data in sql.items() if name < "003"}
    for name, data in early.items():
        (target / name).write_bytes(data)
    monkeypatch.setattr(_MOD, "_migrations_root", lambda: target)
    migrate()
    for name, data in sql.items():
        (target / name).write_bytes(data)
    migrate()
    return ops_store


def _evidence(**overrides: Any) -> Evidence:
    sql = "select count(*) as n from core.ticket where team = $team"
    params: dict[str, Any] = {"team": "a"}
    fields: dict[str, Any] = {
        "query_id": compute_query_id(sql, params, _BUILD),
        "run_id": "run_1",
        "build_id": _BUILD,
        "sql": sql,
        "params": params,
        "result_hash": "a" * 64,
        "row_count": 1,
        "result_sample": [{"n": 3}],
        "executed_at": datetime(2026, 9, 26, 10, tzinfo=UTC),
        "duration_ms": 12,
    }
    fields.update(overrides)
    return Evidence(**fields)


def _finding_row(finding_id: str, status: str = "proposed") -> None:
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, status,"
            " created_at) VALUES (?, 'run_1', 'task_1', 'analyst', 'claim', ?, ?)",
            (finding_id, status, _TS),
        ),
        op="test_insert_finding",
    )


# --- UT05-47 store parts ---------------------------------------------------------------


def test_ut05_47_record_evidence_is_idempotent_and_keeps_first_run(migrated: Path) -> None:
    """UT05-47 inserting the same evidence twice keeps the first run_id; one row survives."""
    ev = _evidence(run_id="run_1")
    assert record_evidence(ev) is True
    assert record_evidence(ev.model_copy(update={"run_id": "run_2"})) is False
    rows = core.read_all("SELECT query_id, run_id FROM evidence")
    assert [(str(r["query_id"]), str(r["run_id"])) for r in rows] == [(ev.query_id, "run_1")]
    assert get_evidence(ev.query_id) == ev


def test_ut05_47_record_evidence_use_dedupes_by_pk_two_tasks(migrated: Path) -> None:
    """UT05-47 two tasks' uses insert two rows; a repeat of one key is ignored."""
    ev = _evidence()
    assert record_evidence(ev) is True
    used_at = datetime(2026, 9, 26, 11, tzinfo=UTC)
    assert record_evidence_use(ev.query_id, "run_1", "task_a", used_at) is True
    assert record_evidence_use(ev.query_id, "run_1", "task_b", used_at) is True
    assert record_evidence_use(ev.query_id, "run_1", "task_a", used_at) is False
    assert record_evidence_use(ev.query_id, "run_1", None, used_at) is True
    rows = core.read_all(
        "SELECT task_id FROM evidence_use WHERE query_id = ? ORDER BY task_id", (ev.query_id,)
    )
    assert [str(r["task_id"]) for r in rows] == ["", "task_a", "task_b"]


# --- UT05-48 get_evidence tamper check and finding_statuses ----------------------------


def test_ut05_48_get_evidence_none_for_missing_and_tampered(migrated: Path) -> None:
    """UT05-48 a missing query_id, a tampered sql row and a tampered build_id row → None."""
    assert get_evidence("q_" + "0" * 16) is None
    good = _evidence()
    assert record_evidence(good) is True
    assert get_evidence(good.query_id) == good

    # A row inserted directly with a mismatching query_id/sql pair (simulated tamper).
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES (?, NULL, ?, ?, '{}', ?, 0, '[]', ?, 1)",
            ("q_2222222222222222", _BUILD, "select 1", "b" * 64, _TS),
        ),
        op="test_insert_tampered",
    )
    with capture_logs() as logs:
        assert get_evidence("q_2222222222222222") is None
    tampered_events = [e for e in logs if e["event"] == "harness.evidence.tampered"]
    assert len(tampered_events) == 1
    assert tampered_events[0]["query_id"] == "q_2222222222222222"
    assert tampered_events[0]["log_level"] == "warning"

    # Corrupt build_id so recompute raises SchemaViolation internally (still treated as tamper).
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES (?, NULL, 'not-a-build', ?, '{}', ?, 0, '[]', ?, 1)",
            ("q_3333333333333333", "select 1", "c" * 64, _TS),
        ),
        op="test_insert_bad_build",
    )
    with capture_logs() as logs:
        assert get_evidence("q_3333333333333333") is None
    assert any(e["event"] == "harness.evidence.tampered" for e in logs)


def test_ut05_48_finding_statuses_dict(migrated: Path) -> None:
    """UT05-48 finding_statuses returns a dict for valid known ids, drops bad-pattern ids."""
    fid1 = "fnd_" + new_ulid()
    fid2 = "fnd_" + new_ulid()
    unknown = "fnd_" + new_ulid()
    _finding_row(fid1, status="proposed")
    _finding_row(fid2, status="verified")
    result = finding_statuses([fid1, fid2, unknown, "not-a-finding-id"])
    assert result == {fid1: "proposed", fid2: "verified"}
    assert finding_statuses([]) == {}
    assert finding_statuses(["bad-id"]) == {}


# --- UT05-126 scrub_record_from_evidence -----------------------------------------------


def test_ut05_126_scrub_removes_only_matching_rows_and_is_idempotent(migrated: Path) -> None:
    """UT05-126 scrub rewrites the two rows holding the record id and keeps the rest; idempotent."""
    record_id = "jira:ticket:ENG-1234"
    ev_equal = _evidence(
        query_id=compute_query_id("select 1", {}, _BUILD),
        sql="select 1",
        params={},
        result_sample=[{"id": record_id}, {"id": "other"}],
    )
    ev_substr = _evidence(
        query_id=compute_query_id("select 2", {}, _BUILD),
        sql="select 2",
        params={},
        result_sample=[{"note": f"see {record_id} for detail"}, {"note": "unrelated"}],
    )
    ev_clean = _evidence(
        query_id=compute_query_id("select 3", {}, _BUILD),
        sql="select 3",
        params={},
        result_sample=[{"note": "no record id here"}],
    )
    for ev in (ev_equal, ev_substr, ev_clean):
        assert record_evidence(ev) is True

    # A row whose sample is not a list (defensive branch: skip, do not update).
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES ('q_4444444444444444', NULL, ?, 'select 4', '{}', ?, 0, ?, ?, 1)",
            (_BUILD, "d" * 64, f'"{record_id}"', _TS),
        ),
        op="test_insert_non_list_sample",
    )
    # A row whose record id occurrence is in a key name, not a value (no cell actually matches).
    core.run_write(
        lambda conn: conn.execute(
            "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
            " row_count, result_sample, executed_at, duration_ms)"
            " VALUES ('q_5555555555555555', NULL, ?, 'select 5', '{}', ?, 0, ?, ?, 1)",
            (_BUILD, "e" * 64, f'[{{"{record_id}": 1}}]', _TS),
        ),
        op="test_insert_key_match",
    )

    with capture_logs() as logs:
        updated = scrub_record_from_evidence(record_id)
    assert updated == 2
    scrubbed = [e for e in logs if e["event"] == "harness.evidence.scrubbed"]
    assert len(scrubbed) == 1
    assert scrubbed[0]["rows"] == 2
    assert record_id not in repr(scrubbed[0])
    assert len(scrubbed[0]["record_id_hash"]) == 16

    got_equal = get_evidence(ev_equal.query_id)
    assert got_equal is not None
    assert got_equal.result_sample == [{"id": "other"}]
    assert got_equal.query_id == ev_equal.query_id
    assert got_equal.result_hash == ev_equal.result_hash
    assert got_equal.row_count == ev_equal.row_count

    got_substr = get_evidence(ev_substr.query_id)
    assert got_substr is not None
    assert got_substr.result_sample == [{"note": "unrelated"}]

    got_clean = get_evidence(ev_clean.query_id)
    assert got_clean is not None
    assert got_clean.result_sample == [{"note": "no record id here"}]

    non_list_row = core.read_one(
        "SELECT result_sample FROM evidence WHERE query_id = 'q_4444444444444444'"
    )
    assert non_list_row is not None
    assert record_id in str(non_list_row["result_sample"])

    key_match_row = core.read_one(
        "SELECT result_sample FROM evidence WHERE query_id = 'q_5555555555555555'"
    )
    assert key_match_row is not None
    assert record_id in str(key_match_row["result_sample"])

    assert scrub_record_from_evidence(record_id) == 0


def test_ut05_126_scrub_rejects_invalid_record_id(migrated: Path) -> None:
    """UT05-126 an id that does not parse as source:entity:key raises SchemaViolation."""
    with pytest.raises(SchemaViolation, match="invalid record_id"):
        scrub_record_from_evidence("not-a-record-id")
