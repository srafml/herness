"""Security test for SQL rendering against injection through config values (ST02-10)."""

from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.model import sqlfiles
from herness.model.lakeinfo import LakeInventory
from herness.model.render_context import RenderContext
from herness.model.settings import (
    CustomFieldsConfig,
    DqSettings,
    MappingsConfig,
    ServiceNowCustomFields,
)
from herness.model.sqlfiles import discover_sql_files, render_sql

pytestmark = pytest.mark.integration

EVIL = 'x" ; DROP TABLE core.incident; --'
ENUM_VALUE = 'it\'s "closed"; DROP TABLE core.incident; --'
BUILD_ID = "20260901-120000-01ABCD"


def _context(root: Path, custom: CustomFieldsConfig) -> RenderContext:
    return RenderContext(
        build_id=BUILD_ID,
        dq=DqSettings(),
        custom_fields=custom,
        lake=LakeInventory(root=root, entities={}),
        extra_entities={},
    )


def _warehouse(path: Path) -> duckdb.DuckDBPyConnection:
    con = duckdb.connect(str(path))
    con.execute("CREATE SCHEMA core")
    con.execute("CREATE SCHEMA stg")
    con.execute(
        "CREATE TABLE core.incident AS SELECT * FROM (VALUES (?), ('open')) t(state)",
        [ENUM_VALUE],
    )
    # enum maps reach DuckDB as Arrow data (U02-87), never as SQL text
    enum_map = pa.table({"source_value": [ENUM_VALUE], "canonical": ["closed"]})
    con.register("_ref_enum_map", enum_map)
    con.execute("CREATE TABLE stg.enum_map AS SELECT * FROM _ref_enum_map")
    con.unregister("_ref_enum_map")
    return con


def _core_tables(con: duckdb.DuckDBPyConnection) -> int:
    row = con.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'core'"
    ).fetchone()
    assert row is not None
    return int(row[0])


def test_st02_10_custom_field_rejected_by_config() -> None:
    """ST02-10 the injected custom field name fails config validation."""
    with pytest.raises(ValidationError):
        CustomFieldsConfig.model_validate({"servicenow": {"customer_impact_minutes": EVIL}})
    with pytest.raises(ValidationError):
        MappingsConfig.model_validate({"custom_fields": {"jira": {"team": EVIL}}})


def test_st02_10_filters_reject_injection(tmp_path: Path) -> None:
    """ST02-10 even bypassing config, ident and raw refuse the injected name."""
    inv = LakeInventory(root=tmp_path, entities={})
    with pytest.raises(ConfigError, match="invalid identifier"):
        sqlfiles._ident(EVIL)
    with pytest.raises(ConfigError):
        sqlfiles._raw(inv, "servicenow", "incident", EVIL, "VARCHAR")


def test_st02_10_render_then_execute(tmp_path: Path) -> None:
    """ST02-10 an unvalidated field cannot render; a quoted enum value matches literally."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "100_bad.sql").write_text(
        "SELECT {{ custom_fields.servicenow.customer_impact_minutes | ident }}"
        " FROM core.incident;\n",
        encoding="utf-8",
    )
    (sql_dir / "110_enum.sql").write_text(
        "SELECT count(*) FROM core.incident i\nJOIN stg.enum_map m ON i.state = m.source_value;\n",
        encoding="utf-8",
    )
    bad, enum_file = discover_sql_files(sql_dir=sql_dir)
    # model_construct skips validation: the filter is the last line of defence
    forged = CustomFieldsConfig.model_construct(
        servicenow=ServiceNowCustomFields.model_construct(customer_impact_minutes=EVIL)
    )
    con = _warehouse(tmp_path / "w.duckdb")
    try:
        with pytest.raises(ConfigError, match=r"render failed for 100_bad\.sql: ConfigError"):
            render_sql(bad, _context(tmp_path, forged))

        rendered = render_sql(enum_file, _context(tmp_path, CustomFieldsConfig()))
        statements = con.extract_statements(rendered)
        assert len(statements) == 1
        assert con.execute(statements[0].query).fetchone() == (1,)

        literal = sqlfiles._sqlstr(ENUM_VALUE)
        query = f"SELECT count(*) FROM stg.enum_map WHERE source_value = {literal}"  # noqa: S608
        assert len(con.extract_statements(query)) == 1
        assert con.execute(query).fetchone() == (1,)
        assert _core_tables(con) == 1
    finally:
        con.close()
