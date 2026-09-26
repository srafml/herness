"""Unit tests for the ops migrations 001-006 as shipped (impl 02 U02-49 … U02-54, §4.3;
UT02-32, UT02-36 … UT02-39; TH02-07 CHECK caps).

The 001-002 parts of UT02-37 live in test_store_ops_migrate.py next to the runner tests.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from herness.core.errors import SchemaViolation
from herness.store.ops import core
from herness.store.ops.migrate import MigrationReport, migrate, schema_version

pytestmark = pytest.mark.unit

_TS = "2026-09-26T10:00:00.000000Z"
_TS2 = "2026-09-26T11:00:00.000000Z"
# Every table named in impl 02 §4.3.1 … §4.3.6 (FTS5 shadow tables are not listed there).
_TABLES_43 = {
    "watermark",
    "sync_slice",
    "file_ingest",
    "source_health",
    "job",
    "worker",
    "resilience_event",
    "run",
    "task",
    "finding",
    "evidence",
    "evidence_use",
    "memory_item",
    "memory_fts",
    "recommendation",
    "decision_log",
    "outcome",
    "review_item",
    "chat_session",
    "chat_message",
    "deletion_request",
    "metric_sample",
}
_INDEXES = {
    "sync_slice_status",
    "job_idem_active",
    "job_claim",
    "resilience_event_kind_ts",
    "resilience_event_ts",
    "task_dedup",
    "task_run_status",
    "finding_run_status",
    "evidence_run",
    "run_status",
    "memory_item_layer_status",
    "memory_item_kind_status",
    "recommendation_run",
    "decision_log_rec",
    "outcome_rec_measurement",
    "review_item_status_kind",
    "chat_session_user",
    "chat_session_active",
    "chat_message_session",
    "deletion_request_status",
    "deletion_request_record",
    "metric_sample_name_ts",
    "metric_sample_ts",
}
# Indexes of later owners' migrations (R-11): 090_chat (impl 09 U09-43).
_LATER_INDEXES = {"chat_message_reply"}
_TRIGGERS = {"memory_item_ai", "memory_item_ad", "memory_item_au"}


def _write(sql: str, params: tuple[object, ...] = ()) -> None:
    core.run_write(lambda c: c.execute(sql, params), op="test_write")


def _schema(kind: str) -> set[str]:
    rows = core.read_all(
        "SELECT name FROM sqlite_schema WHERE type = ? AND name NOT LIKE 'sqlite_%'", (kind,)
    )
    return {str(r["name"]) for r in rows}


@pytest.fixture
def migrated(ops_store: Path) -> Path:
    migrate()
    return ops_store


# --- UT02-32 --------------------------------------------------------------------------------


def test_ut02_32_fresh_store_gets_the_43_table_set(ops_store: Path) -> None:
    """UT02-32 a fresh store: table set equals §4.3 plus `schema_migration`; six rows recorded.

    Later owners' files (R-11: 090_chat) follow the six impl 02 rows and add no table."""
    report = migrate()
    assert isinstance(report, MigrationReport)
    assert report.duration_ms >= 0
    assert report.applied[:6] == (
        "001_ingestion_health",
        "002_jobs",
        "003_runs_evidence",
        "004_memory",
        "005_review_chat_privacy",
        "006_metric_sample",
    )
    assert report.version == schema_version() == int(report.applied[-1][:3])
    assert all(int(name[:3]) >= 10 for name in report.applied[6:])
    rows = core.read_all(
        "SELECT version, name, checksum, applied_at FROM schema_migration ORDER BY version"
    )
    assert [(r["version"], r["name"]) for r in rows][:6] == [
        (1, "ingestion_health"),
        (2, "jobs"),
        (3, "runs_evidence"),
        (4, "memory"),
        (5, "review_chat_privacy"),
        (6, "metric_sample"),
    ]
    assert all(len(str(r["checksum"])) == 64 for r in rows)
    listed = core.read_all(
        "SELECT name, type FROM pragma_table_list WHERE schema = 'main'"
        " AND type IN ('table', 'virtual') AND name NOT LIKE 'sqlite_%'"
    )
    assert {str(r["name"]) for r in listed} == _TABLES_43 | {"schema_migration"}
    assert {str(r["name"]) for r in listed if r["type"] == "virtual"} == {"memory_fts"}
    assert _schema("index") == _INDEXES | _LATER_INDEXES
    assert _schema("trigger") == _TRIGGERS


def test_ut02_32_ordinary_tables_are_strict(migrated: Path) -> None:
    """UT02-32 every ordinary table of §4.3 is STRICT (§3.6 Tables rule)."""
    rows = core.read_all(
        "SELECT name, strict FROM pragma_table_list WHERE schema = 'main' AND type = 'table'"
        " AND name NOT LIKE 'sqlite_%'"
    )
    strict = {str(r["name"]): int(r["strict"]) for r in rows}
    for table in _TABLES_43 - {"memory_fts"}:
        assert strict[table] == 1, table


def test_ut02_32_foreign_keys_and_unique_expression_index(migrated: Path) -> None:
    """UT02-32 the FKs of §4.3.3 … §4.3.5 and the `task_dedup` expression index exist."""
    fks = {
        (table, str(r["table"]), str(r["from"]), str(r["on_delete"]))
        for table in ("task", "decision_log", "outcome", "chat_message")
        for r in core.read_all("SELECT * FROM pragma_foreign_key_list(?)", (table,))
    }
    assert fks == {
        ("task", "run", "run_id", "NO ACTION"),
        ("decision_log", "recommendation", "rec_id", "NO ACTION"),
        ("outcome", "recommendation", "rec_id", "NO ACTION"),
        ("chat_message", "chat_session", "session_id", "CASCADE"),
    }
    row = core.read_one("SELECT sql FROM sqlite_schema WHERE name = 'task_dedup'")
    assert row is not None
    assert "UNIQUE" in str(row["sql"])
    assert "json_extract(spec, '$.dedup_key')" in str(row["sql"])


# --- fixtures of rows -----------------------------------------------------------------------

_RUN = (
    "INSERT INTO run (run_id, kind, depth, profile, config_hash, started_at)"
    " VALUES (?, 'funding_review', 'standard', 'default', 'h', ?)"
)
_TASK = (
    "INSERT INTO task (task_id, run_id, role, spec, created_at, updated_at)"
    " VALUES (?, ?, 'analyst', ?, ?, ?)"
)
_MEMORY = (
    "INSERT INTO memory_item (memory_id, layer, kind, content, status, created_at)"
    " VALUES (?, 'semantic', ?, ?, 'active', ?)"
)
_REVIEW = (
    "INSERT INTO review_item (item_id, kind, payload, status, created_at, decided_by, note,"
    " decided_at) VALUES (?, 'mapping_suggestion', ?, ?, ?, ?, ?, ?)"
)
_SESSION = (
    "INSERT INTO chat_session (session_id, user_ref, created_at, last_active_at)"
    " VALUES (?, ?, ?, ?)"
)
_MESSAGE = (
    "INSERT INTO chat_message (message_id, session_id, role, status, created_at)"
    " VALUES (?, ?, 'user', 'done', ?)"
)
_METRIC = (
    "INSERT INTO metric_sample (ts, name, kind, value, labels, component)"
    " VALUES (?, ?, ?, 1.0, ?, 'jobs')"
)
_EVIDENCE = (
    "INSERT INTO evidence (query_id, build_id, sql, result_hash, params, row_count,"
    " duration_ms, executed_at) VALUES (?, 'b1', 'SELECT 1', 'h', '{}', 1, 2, ?)"
)
_USER = "u" * 32  # user_ref is a 32-char pseudonym


# --- UT02-36 memory_fts ---------------------------------------------------------------------


def _fts(term: str) -> list[str]:
    rows = core.read_all(
        "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
        " WHERE memory_fts MATCH ? ORDER BY m.memory_id",
        (term,),
    )
    return [str(r["memory_id"]) for r in rows]


def test_ut02_36_memory_fts_follows_insert_update_delete(migrated: Path) -> None:
    """UT02-36 `memory_fts MATCH` follows insert, content update and delete of `memory_item`."""
    _write(_MEMORY, ("m1", "incident_pattern", "Payroll batch fails on Monday", _TS))
    _write(_MEMORY, ("m2", "team_note", "Network latency spikes", _TS))
    assert _fts("payroll") == ["m1"]
    assert _fts("kind:team_note") == ["m2"]
    _write("UPDATE memory_item SET content = 'Café invoices délayed' WHERE memory_id = 'm1'")
    assert _fts("payroll") == []
    assert _fts("delayed") == ["m1"]  # remove_diacritics 2
    assert _fts("cafe") == ["m1"]
    _write("UPDATE memory_item SET kind = 'runbook' WHERE memory_id = 'm2'")
    assert _fts("kind:team_note") == []
    assert _fts("kind:runbook") == ["m2"]
    _write("UPDATE memory_item SET use_count = use_count + 1 WHERE memory_id = 'm2'")
    assert _fts("latency") == ["m2"]
    _write("DELETE FROM memory_item WHERE memory_id = 'm1'")
    assert _fts("delayed") == []
    assert _fts("latency") == ["m2"]
    integrity = "INSERT INTO memory_fts(memory_fts, rank) VALUES ('integrity-check', 1)"
    _write(integrity)  # raises SchemaViolation if the index and memory_item disagree


# --- UT02-37 003-006 constraints (and TH02-07 CHECK caps) -----------------------------------


@pytest.mark.parametrize(
    ("sql", "params"),
    [
        # bad enum
        (_RUN.replace("'standard'", "'shallow'"), ("r2", _TS)),
        (_MEMORY.replace("'semantic'", "'dream'"), ("m1", "k", "c", _TS)),
        (_REVIEW, ("i1", "{}", "maybe", _TS, "alice", None, _TS)),  # decided: only the enum
        (_MESSAGE.replace("'user'", "'bot'"), ("x1", "s1", _TS)),
        (_METRIC, (_TS, "herness_jobs_total", "summary", "{}")),
        # bad JSON
        (_TASK, ("t1", "r1", "{nope", _TS, _TS)),
        (_REVIEW, ("i1", "[1]", "pending", _TS, None, None, None)),
        (_METRIC, (_TS, "herness_jobs_total", "counter", '["a"]')),
        # bad timestamp
        (_RUN, ("r2", "2026-09-26T10:00:00Z")),
        (_MEMORY, ("m1", "k", "c", "2026-09-26")),
        (_EVIDENCE, ("q_0123456789abcdef", "2026-09-26 10:00:00.000000Z")),
        # other design checks
        (_EVIDENCE, ("q_A123456789abcdef", _TS)),
        (_EVIDENCE, ("q_0123", _TS)),
        (_METRIC, (_TS, "jobs_total", "counter", "{}")),
        (_SESSION, ("s2", "short", _TS, _TS)),
        (_REVIEW, ("i1", "{}", "approved", _TS, None, None, None)),
        (_REVIEW, ("i1", "{}", "pending", _TS, "alice", None, None)),
        (_REVIEW, ("i1", "{}", "pending", _TS, "alice", None, _TS)),
        # TH02-07 caps: payload > 65536, note > 2000, labels > 1024
        (_REVIEW, ("i1", '{"v":"' + "x" * 65_530 + '"}', "pending", _TS, None, None, None)),
        (_REVIEW, ("i1", "{}", "approved", _TS, "alice", "n" * 2001, _TS)),
        (_METRIC, (_TS, "herness_jobs_total", "counter", '{"v":"' + "x" * 1020 + '"}')),
    ],
)
def test_ut02_37_003_006_reject_bad_values(
    migrated: Path, sql: str, params: tuple[object, ...]
) -> None:
    """UT02-37 (003-006 part) bad enum, JSON, timestamp, pattern, pairing or size is rejected
    by a CHECK (fresh primary keys, so no PK conflict can stand in for the CHECK)."""
    _write(_RUN, ("r1", _TS))  # parent rows for the task / message cases
    _write(_SESSION, ("s1", _USER, _TS, _TS))
    with pytest.raises(SchemaViolation, match="CHECK constraint failed"):
        _write(sql, params)


def test_ut02_37_003_006_accept_boundary_values(migrated: Path) -> None:
    """UT02-37 the caps are inclusive: 65536-byte payload, 2000-char note, 1024-byte labels."""
    payload = '{"v":"' + "x" * (65_536 - 8) + '"}'
    assert len(payload) == 65_536
    _write(_REVIEW, ("i1", payload, "pending", _TS, None, None, None))
    _write(_REVIEW, ("i2", "{}", "approved", _TS, "alice", "n" * 2000, _TS2))
    labels = '{"v":"' + "x" * (1024 - 8) + '"}'
    _write(_METRIC, (_TS, "herness_jobs_total", "histogram", labels))
    _write(_EVIDENCE, ("q_0123456789abcdef", _TS))
    row = core.read_one("SELECT count(*) AS n FROM metric_sample")
    assert row is not None
    assert row["n"] == 1


# --- UT02-38 task dedup ---------------------------------------------------------------------


def test_ut02_38_same_dedup_key_in_one_run_is_unique(migrated: Path) -> None:
    """UT02-38 a second task with the same run and `dedup_key` fails `UNIQUE`."""
    _write(_RUN, ("r1", _TS))
    _write(_RUN, ("r2", _TS))
    _write(_TASK, ("t1", "r1", '{"dedup_key": "svc-a"}', _TS, _TS))
    with pytest.raises(SchemaViolation, match="UNIQUE"):
        _write(_TASK, ("t2", "r1", '{"dedup_key": "svc-a", "x": 1}', _TS, _TS))
    _write(_TASK, ("t3", "r2", '{"dedup_key": "svc-a"}', _TS, _TS))  # other run
    _write(_TASK, ("t4", "r1", "{}", _TS, _TS))  # no key: NULLs never collide
    _write(_TASK, ("t5", "r1", "{}", _TS, _TS))


# --- UT02-39 FKs ----------------------------------------------------------------------------


def test_ut02_39_task_needs_run_and_session_delete_cascades(migrated: Path) -> None:
    """UT02-39 a task with an unknown `run_id` is an FK error; deleting a session deletes its
    messages."""
    with pytest.raises(SchemaViolation, match="FOREIGN KEY"):
        _write(_TASK, ("t1", "missing", "{}", _TS, _TS))
    _write(_SESSION, ("s1", _USER, _TS, _TS))
    _write(_SESSION, ("s2", _USER, _TS, _TS))
    for i, session in enumerate(("s1", "s1", "s2")):
        _write(_MESSAGE, (f"x{i}", session, _TS))
    _write("DELETE FROM chat_session WHERE session_id = 's1'")
    rows = core.read_all("SELECT message_id FROM chat_message ORDER BY message_id")
    assert [str(r["message_id"]) for r in rows] == ["x2"]
