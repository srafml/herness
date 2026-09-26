"""Unit tests for the Jinja macros of the build SQL (UT02-60 for U02-106).

A staging-shaped template in a temp SQL directory imports the real `_macros.jinja` and runs
through `build_harness` after the real setup files, over two tiny lake files.
"""

import datetime
import shutil
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.support.build_harness import BuildHarness, RefData

from herness.model import sqlfiles
from herness.model.lakeinfo import scan_lake
from herness.model.settings import MappingsConfig

pytestmark = pytest.mark.unit

UTC = datetime.UTC
T1 = datetime.datetime(2024, 1, 1, tzinfo=UTC)
T2 = datetime.datetime(2024, 1, 2, tzinfo=UTC)
T3 = datetime.datetime(2024, 1, 3, tzinfo=UTC)

TEMPLATE = """\
{% import "_macros.jinja" as m %}
CREATE OR REPLACE TABLE stg.t AS
SELECT l._record_id, {{ m.rid('servicenow:sys_user_group', 'l.assignment_group') }} AS team_id,
    {{ m.typed('l.opened_at', 'ts_utc(l.opened_at)', 'opened_at') }},
    e.canonical AS state, l.missing_col
FROM {{ m.latest('servicenow', 'incident', [('state', 'VARCHAR'), ('opened_at', 'VARCHAR'),
    ('assignment_group', 'VARCHAR'), ('missing_col', 'BIGINT')]) }} AS l
{{ m.enum('e', 'servicenow.incident_state', 'l.state') }};
{{ m.cast_stats('t', ['opened_at']) }};
{{ m.cast_stats('t', []) }};
CREATE OR REPLACE TABLE stg.t_absent AS
SELECT * FROM {{ m.latest('servicenow', 'problem', [('x', 'VARCHAR'), ('n', 'BIGINT')]) }} AS a;
"""


def _lake_file(path: Path, rows: list[dict[str, object]]) -> None:
    ts = pa.timestamp("us", tz="UTC")
    schema = pa.schema(
        [
            ("_record_id", pa.string()),
            ("_source_key", pa.string()),
            ("_source_updated_at", ts),
            ("_fetched_at", ts),
            ("_deleted", pa.bool_()),
            ("state", pa.string()),
            ("opened_at", pa.string()),
            ("assignment_group", pa.string()),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.Table.from_pylist(rows, schema=schema), path)


def _row(key: str, updated: datetime.datetime, fetched: datetime.datetime, **cols: object) -> dict:
    return {
        "_record_id": f"servicenow:incident:{key}",
        "_source_key": key,
        "_source_updated_at": updated,
        "_fetched_at": fetched,
        "_deleted": cols.pop("deleted", False),
        "state": cols.get("state"),
        "opened_at": cols.get("opened_at"),
        "assignment_group": cols.get("group"),
    }


@pytest.fixture
def sql_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "sql"
    directory.mkdir()
    shutil.copy(Path(sqlfiles.__file__).parent / "sql" / "_macros.jinja", directory)
    (directory / "100_t.sql").write_text(TEMPLATE, encoding="utf-8")
    return directory


def _write_lake(raw: Path) -> None:
    base = raw / "servicenow" / "incident" / "date=2024-01-01"
    old = {"state": "New", "opened_at": "2024-01-01 00:00:00", "group": "g0"}
    _lake_file(
        base / "a.parquet",
        [
            _row("inc1", T1, T1, **old),
            _row("inc2", T1, T1, **old),
            _row("inc3", T1, T1, **old),
            _row("inc5", T2, T1, state="New"),
        ],
    )
    _lake_file(
        base / "b.parquet",
        [
            _row("inc1", T2, T2, state="Resolved", opened_at="2024-01-01 08:00:00", group=" g1 "),
            _row("inc2", T2, T2, deleted=True),
            _row("inc4", T1, T1, state="Resolved ", opened_at="garbage", group=""),
            _row("inc5", T2, T3, state="RESOLVED", opened_at="2024-01-02T00:00:00Z"),
        ],
    )


def test_ut02_60_jinja_macros_stage_a_lake_entity(
    build_harness: BuildHarness, sql_dir: Path
) -> None:
    """UT02-60 latest/typed/enum/rid/cast_stats: dedupe, tombstones, deletions, flags."""
    build_harness.run(0, 99)
    _write_lake(build_harness.layout.raw)
    inventory = scan_lake(build_harness.layout)
    refdata = RefData(
        mappings=MappingsConfig.model_validate(
            {"enums": {"servicenow.incident_state": {"Resolved": "resolved"}}}
        ),
        deleted_ids=["servicenow:incident:inc3"],
    )
    ran = build_harness.run(100, 199, inventory=inventory, refdata=refdata, sql_dir=sql_dir)
    assert ran == ["100_t.sql"]
    assert build_harness.refdata_counts is not None
    assert build_harness.refdata_counts["stg.deleted_record"] == 1

    rows = build_harness.query(
        'SELECT _record_id, team_id, opened_at, "_nn_opened_at", "_cf_opened_at", state,'
        " missing_col FROM stg.t ORDER BY _record_id"
    )
    assert rows == [
        (
            "servicenow:incident:inc1",
            "servicenow:sys_user_group:g1",
            datetime.datetime(2024, 1, 1, 8, tzinfo=UTC),
            True,
            False,
            "resolved",
            None,
        ),
        ("servicenow:incident:inc4", None, None, True, True, None, None),
        (
            "servicenow:incident:inc5",
            None,
            datetime.datetime(2024, 1, 2, tzinfo=UTC),
            True,
            False,
            "resolved",
            None,
        ),
    ]
    assert build_harness.query("SELECT * FROM stg.cast_stats") == [("stg.t", "opened_at", 3, 1)]


def test_ut02_60_latest_absent_entity_is_typed_and_empty(
    build_harness: BuildHarness, sql_dir: Path
) -> None:
    """UT02-60 an absent entity reads as zero rows with the declared names and types."""
    build_harness.run(0, 99)
    build_harness.run(100, 199, refdata=RefData(), sql_dir=sql_dir)
    assert build_harness.query("SELECT count(*) FROM stg.t") == [(0,)]
    columns = build_harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'stg' AND table_name = 't_absent' ORDER BY ordinal_position"
    )
    assert columns == [
        ("_record_id", "VARCHAR"),
        ("_source_key", "VARCHAR"),
        ("_source_updated_at", "TIMESTAMP WITH TIME ZONE"),
        ("x", "VARCHAR"),
        ("n", "BIGINT"),
    ]
    assert build_harness.query("SELECT count(*) FROM stg.t_absent") == [(0,)]


def test_ut02_60_rendered_macros_quote_values(build_harness: BuildHarness, sql_dir: Path) -> None:
    """UT02-60 macro output carries names through ident and literals through sqlstr."""
    rendered = build_harness.render(100, 100, sql_dir=sql_dir)["100_t.sql"]
    assert 'stg.enum_map AS "e" ON "e".domain = \'servicenow.incident_state\'' in rendered
    assert "'servicenow:sys_user_group' || ':' || trim(l.assignment_group)" in rendered
    assert '(l.opened_at IS NOT NULL) AS "_nn_opened_at"' in rendered
    assert "SELECT NULL, NULL, 0, 0 WHERE false" in rendered
    assert 'CAST(NULL AS BIGINT) AS "missing_col"' in rendered
