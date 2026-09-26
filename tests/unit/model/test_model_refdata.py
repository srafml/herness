"""Unit tests for reference-table registration (UT02-59: U02-87, U02-128)."""

from collections.abc import Iterator, Mapping
from datetime import UTC, datetime
from types import MappingProxyType

import duckdb
import pytest
import structlog

from herness.core.errors import SchemaViolation
from herness.model import refdata
from herness.model.refdata import register_prev_row_counts, register_reference_tables
from herness.model.settings import BuildSettings, MappingsConfig
from herness.store.ops import ReviewItem

pytestmark = pytest.mark.unit

T0 = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
SVC_A = "servicenow:cmdb_ci:a1"
SVC_B = "servicenow:cmdb_ci:b2"
TEAM = "servicenow:sys_user_group:g1"


def _item(item_id: str, payload: Mapping[str, object]) -> ReviewItem:
    return ReviewItem(
        item_id=item_id,
        kind="mapping_suggestion",
        payload=MappingProxyType(dict(payload)),
        status="approved",
        created_at=T0,
        decided_by="lead",
        decided_at=T0,
        note=None,
    )


MAPPINGS = MappingsConfig.model_validate(
    {
        "enums": {
            "servicenow.change_type": {"Emergency": "emergency", "EMERGENCY": "emergency"},
            "servicenow.incident_state": {"6": "resolved", 'It\'s "Closed"': "closed"},
        },
        "service_overrides": [
            {
                "service_id": SVC_A,
                "team_id": TEAM,
                "jira_project": "PAY",
                "jira_component": "Checkout",
                "aliases": ["Payments", "PAYMENTS", "pay-api"],
            },
            {"service_id": SVC_B, "org_id": "servicenow:cmn_department:d1", "role": "support"},
        ],
    }
)
BUILD_CFG = BuildSettings(memory_limit="1GB", service_ci_classes=["cmdb_ci_service", "app"])
APPROVED = (
    _item(
        "r1",
        {
            "subject_type": "jira_component",
            "jira_project": "OPS",
            "jira_component": "Pager",
            "team_id": None,
            "service_id": SVC_A,
            "score": 0.9,
        },
    ),
    _item("r2", {"subject_type": "team", "team_id": TEAM, "service_id": SVC_B, "score": 1}),
    _item("r3", {"subject_type": "team", "team_id": TEAM, "score": 0.5}),  # no service_id
    _item("r4", {"subject_type": "service", "service_id": SVC_A, "score": 0.5}),
    _item("r5", {"subject_type": "team", "service_id": SVC_A, "team_id": 7}),
    _item("r6", {"subject_type": "team", "service_id": SVC_A, "score": True}),
    _item("r7", {"subject_type": "team", "service_id": SVC_A, "score": float("nan")}),
    _item("r8", {"subject_type": "team", "service_id": ""}),
)


@pytest.fixture
def con() -> Iterator[duckdb.DuckDBPyConnection]:
    connection = duckdb.connect()
    connection.execute("CREATE SCHEMA stg")
    try:
        yield connection
    finally:
        connection.close()


def _rows(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[object, ...]]:
    return con.execute(f"SELECT * FROM {table} ORDER BY ALL").fetchall()  # noqa: S608


def _columns(con: duckdb.DuckDBPyConnection, table: str) -> list[tuple[str, str]]:
    schema, name = table.split(".")
    return [
        (str(r[0]), str(r[1]))
        for r in con.execute(
            "SELECT column_name, data_type FROM information_schema.columns"
            " WHERE table_schema = ? AND table_name = ? ORDER BY ordinal_position",
            [schema, name],
        ).fetchall()
    ]


def _register(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    return register_reference_tables(
        con,
        mappings=MAPPINGS,
        build_cfg=BUILD_CFG,
        deleted_ids=["servicenow:incident:x", "jira:issue:J-1", "servicenow:incident:x"],
        approved=APPROVED,
    )


def test_ut02_59_six_tables_with_expected_rows(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 enums, overrides, aliases, classes, deletions and approved items become tables."""
    counts = _register(con)
    assert counts == {
        "stg.enum_map": 3,
        "stg.service_override": 2,
        "stg.service_alias": 2,
        "stg.service_ci_class": 2,
        "stg.deleted_record": 2,
        "stg.approved_mapping": 2,
    }
    assert _rows(con, "stg.enum_map") == [
        ("servicenow.change_type", "emergency", "emergency"),
        ("servicenow.incident_state", "6", "resolved"),
        ("servicenow.incident_state", 'it\'s "closed"', "closed"),
    ]
    assert _rows(con, "stg.service_override") == [
        (SVC_A, TEAM, "PAY", "Checkout", None, "owner"),
        (SVC_B, None, None, None, "servicenow:cmn_department:d1", "support"),
    ]
    assert _rows(con, "stg.service_alias") == [("pay-api", SVC_A), ("payments", SVC_A)]
    assert _rows(con, "stg.service_ci_class") == [("app",), ("cmdb_ci_service",)]
    assert _rows(con, "stg.deleted_record") == [("jira:issue:J-1",), ("servicenow:incident:x",)]
    assert _rows(con, "stg.approved_mapping") == [
        ("r1", "jira_component", "OPS", "Pager", None, SVC_A, 0.9),
        ("r2", "team", None, None, TEAM, SVC_B, 1.0),
    ]


def test_ut02_59_exact_columns(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 every table has exactly the declared columns and types, also when empty."""
    register_reference_tables(
        con, mappings=MappingsConfig(), build_cfg=BuildSettings(), deleted_ids=(), approved=()
    )
    v = "VARCHAR"
    assert _columns(con, "stg.enum_map") == [
        ("domain", v),
        ("source_value_lc", v),
        ("canonical", v),
    ]
    assert _columns(con, "stg.service_override") == [
        (c, v)
        for c in ("service_id", "team_id", "jira_project", "jira_component", "org_id", "role")
    ]
    assert _columns(con, "stg.service_alias") == [("alias_lc", v), ("service_id", v)]
    assert _columns(con, "stg.service_ci_class") == [("ci_class", v)]
    assert _columns(con, "stg.deleted_record") == [("record_id", v)]
    assert _columns(con, "stg.approved_mapping") == [
        *((c, v) for c in ("item_id", "subject_type", "jira_project", "jira_component")),
        ("team_id", v),
        ("service_id", v),
        ("score", "DOUBLE"),
    ]
    assert _rows(con, "stg.enum_map") == []
    assert _rows(con, "stg.service_ci_class") == [
        ("cmdb_ci_service",),
        ("cmdb_ci_service_business",),
        ("cmdb_ci_service_technical",),
    ]


def test_ut02_59_invalid_items_skipped_and_counted(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 unusable approved items are skipped; the count is logged once as a warning."""
    with structlog.testing.capture_logs() as logs:
        _register(con)
    skipped = [e for e in logs if e["event"] == "model.build.mapping_skipped"]
    assert len(skipped) == 1
    assert skipped[0]["log_level"] == "warning"
    assert skipped[0]["count"] == 6
    done = next(e for e in logs if e["event"] == "model.build.refdata_registered")
    assert done["log_level"] == "info"
    assert done["approved_mapping"] == 2
    assert done["enum_map"] == 3


def test_ut02_59_no_skip_warning_when_all_valid(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 with only valid items no mapping_skipped event is logged; no score is NULL."""
    unscored = _item("r9", {"subject_type": "team", "team_id": TEAM, "service_id": SVC_A})
    with structlog.testing.capture_logs() as logs:
        register_reference_tables(
            con,
            mappings=MappingsConfig(),
            build_cfg=BUILD_CFG,
            deleted_ids=set(),
            approved=(*APPROVED[:2], unscored),
        )
    assert all(e["event"] != "model.build.mapping_skipped" for e in logs)
    assert con.execute(
        "SELECT service_id, score FROM stg.approved_mapping WHERE item_id = 'r9'"
    ).fetchall() == [(SVC_A, None)]


def test_ut02_59_rerun_replaces_tables(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 a second registration replaces the rows and leaves no Arrow view behind."""
    _register(con)
    register_reference_tables(
        con, mappings=MappingsConfig(), build_cfg=BUILD_CFG, deleted_ids=(), approved=()
    )
    assert _rows(con, "stg.deleted_record") == []
    assert _rows(con, "stg.approved_mapping") == []
    views = con.execute("SELECT count(*) FROM duckdb_views() WHERE view_name LIKE '_ref_%'")
    assert views.fetchone() == (0,)


def test_ut02_59_duckdb_error_becomes_schema_violation() -> None:
    """UT02-59 a DuckDB failure (no stg schema) is SchemaViolation naming the table only."""
    connection = duckdb.connect()
    try:
        with pytest.raises(SchemaViolation, match=r"refdata registration failed: stg\.enum_map"):
            _register(connection)
        with pytest.raises(SchemaViolation, match=r"stg.prev_row_counts"):
            register_prev_row_counts(connection, {"core.incident": 1})
        assert connection.execute(
            "SELECT count(*) FROM duckdb_views() WHERE view_name LIKE '_ref_%'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_ut02_59_prev_row_counts(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 prev row counts keep `schema.table` keys with count values only (U02-128)."""
    counts: dict[str, object] = {
        "core.incident": 100,
        "core.team": 0,
        "Core.Bad": 5,
        "core": 3,
        "core.x; DROP": 1,
        "core.flag": True,
        "core.neg": -1,
        "core.big": 2**63,
        "metrics.metric_daily": 7,
    }
    register_prev_row_counts(con, counts)  # type: ignore[arg-type]
    assert _columns(con, "stg.prev_row_counts") == [
        ("table_name", "VARCHAR"),
        ("row_count", "BIGINT"),
    ]
    assert _rows(con, "stg.prev_row_counts") == [
        ("core.incident", 100),
        ("core.team", 0),
        ("metrics.metric_daily", 7),
    ]


def test_ut02_59_prev_row_counts_none_is_empty(con: duckdb.DuckDBPyConnection) -> None:
    """UT02-59 `None` (no promoted build) gives an empty stg.prev_row_counts."""
    register_prev_row_counts(con, {"core.incident": 1})
    register_prev_row_counts(con, None)
    assert _rows(con, "stg.prev_row_counts") == []


def test_ut02_59_schemas_cover_every_table() -> None:
    """UT02-59 the module declares exactly the six U02-87 tables."""
    assert set(refdata._SCHEMAS) == {
        "enum_map",
        "service_override",
        "service_alias",
        "service_ci_class",
        "deleted_record",
        "approved_mapping",
    }
