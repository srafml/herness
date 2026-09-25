"""Tests for herness.model.sqlfiles and render_context (U02-82 … U02-86, TH02-11)."""

import datetime
import math
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from herness.core.errors import ConfigError
from herness.model import sqlfiles
from herness.model.lakeinfo import EntityInventory, LakeInventory
from herness.model.render_context import RenderContext, build_render_context
from herness.model.settings import CustomFieldsConfig, DqSettings
from herness.model.sqlfiles import SqlFile, discover_sql_files, render_sql

pytestmark = pytest.mark.unit

BUILD_ID = "20260901-120000-01ABCD"


def _inventory(root: Path, columns: frozenset[str] = frozenset()) -> LakeInventory:
    incident = EntityInventory(
        source="servicenow",
        entity="incident",
        glob=f"{root.as_posix()}/servicenow/incident/**/[!.]*.parquet",
        present=bool(columns),
        files=1 if columns else 0,
        bytes=10 if columns else 0,
        columns=columns,
        from_synth=False,
    )
    return LakeInventory(root=root, entities={("servicenow", "incident"): incident})


def _context(root: Path, columns: frozenset[str] = frozenset()) -> RenderContext:
    return RenderContext(
        build_id=BUILD_ID,
        dq=DqSettings(),
        custom_fields=CustomFieldsConfig(),
        lake=_inventory(root, columns),
        extra_entities={"files": ("budget",)},
    )


def _sql_file(tmp_path: Path, name: str, body: str) -> SqlFile:
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir(exist_ok=True)
    (sql_dir / name).write_text(body, encoding="utf-8")
    return next(f for f in discover_sql_files(sql_dir=sql_dir) if f.name == name)


# --- UT02-56 discovery -----------------------------------------------------------------


def test_ut02_56_discover_order_and_stages(tmp_path: Path) -> None:
    """UT02-56 files sorted by number, `_macros.jinja` and `_x.sql` ignored, stages mapped."""
    names = ["200_core.sql", "000_setup.sql", "950_dq.sql", "100_stg.sql", "310_a.sql"]
    names += ["410_facts.sql", "_macros.jinja", "_helper.sql", "notes.txt"]
    for name in names:
        (tmp_path / name).write_text("SELECT 1;\n", encoding="utf-8")
    files = discover_sql_files(sql_dir=tmp_path)
    assert [f.name for f in files] == [
        "000_setup.sql",
        "100_stg.sql",
        "200_core.sql",
        "310_a.sql",
        "410_facts.sql",
        "950_dq.sql",
    ]
    assert [f.stage for f in files] == ["setup", "staging", "core", "attach", "facts", "dq"]
    assert files[0] == SqlFile(number=0, name="000_setup.sql", path=tmp_path / "000_setup.sql")
    assert [f.number for f in files] == [0, 100, 200, 310, 410, 950]


@pytest.mark.parametrize("name", ["510_x.sql", "899_x.sql", "12_x.sql", "100_Bad.sql", "100-x.sql"])
def test_ut02_56_unexpected_name(tmp_path: Path, name: str) -> None:
    """UT02-56 a name outside the pattern or in 500-899 raises ConfigError."""
    (tmp_path / name).write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match=f"unexpected build SQL file {name}"):
        discover_sql_files(sql_dir=tmp_path)


def test_ut02_56_duplicate_number(tmp_path: Path) -> None:
    """UT02-56 two files sharing a number raise ConfigError naming it."""
    (tmp_path / "100_a.sql").write_text("", encoding="utf-8")
    (tmp_path / "100_b.sql").write_text("", encoding="utf-8")
    with pytest.raises(ConfigError, match="duplicate build SQL number 100"):
        discover_sql_files(sql_dir=tmp_path)


def test_ut02_56_default_package_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-56 with no argument the package directory herness/model/sql is listed."""
    assert Path(sqlfiles.__file__).parent / "sql" == sqlfiles._SQL_DIR
    (tmp_path / "000_setup.sql").write_text("SELECT 1;\n", encoding="utf-8")
    monkeypatch.setattr(sqlfiles, "_SQL_DIR", tmp_path)
    assert [f.path for f in discover_sql_files()] == [tmp_path / "000_setup.sql"]
    monkeypatch.setattr(sqlfiles, "_SQL_DIR", tmp_path / "absent")
    assert discover_sql_files() == []


def test_ut02_56_sqlfile_number_range() -> None:
    """UT02-56 SqlFile rejects a number outside a stage range."""
    with pytest.raises(ValueError, match="stage"):
        SqlFile(number=600, name="600_x.sql", path=Path("600_x.sql"))


# --- UT02-57 rendering and context ------------------------------------------------------


def test_ut02_57_render_config_values(tmp_path: Path) -> None:
    """UT02-57 a template sees only the context values and renders them through filters."""
    body = (
        "SELECT {{ dq.row_count_drop_max | num }} AS m, {{ build_id | sqlstr }} AS b\n"
        "FROM read_parquet({{ lake.get('servicenow', 'incident').glob | sqlstr }});\n"
        "-- {{ raw_root }} {{ extra_entities['files'][0] }}\n"
    )
    file = _sql_file(tmp_path, "100_stg.sql", body)
    ctx = _context(tmp_path / "raw")
    out = render_sql(file, ctx)
    assert out.startswith(f"SELECT 0.05 AS m, '{BUILD_ID}' AS b\n")
    assert f"-- {(tmp_path / 'raw').as_posix()} budget\n" in out
    assert out.endswith("\n")


def test_ut02_57_undefined_var(tmp_path: Path) -> None:
    """UT02-57 an undefined variable raises ConfigError naming the file and line."""
    file = _sql_file(tmp_path, "100_stg.sql", "SELECT 1;\nSELECT {{ nope }};\n")
    with pytest.raises(
        ConfigError, match=r"^render failed for 100_stg\.sql: UndefinedError at line 2$"
    ):
        render_sql(file, _context(tmp_path))


def test_ut02_57_syntax_error(tmp_path: Path) -> None:
    """UT02-57 a template syntax error raises ConfigError with its line."""
    file = _sql_file(tmp_path, "100_stg.sql", "SELECT 1;\n\n{% if %}\n")
    with pytest.raises(
        ConfigError, match=r"render failed for 100_stg\.sql: TemplateSyntaxError at line 3"
    ):
        render_sql(file, _context(tmp_path))


def test_ut02_57_filter_error_mapped(tmp_path: Path) -> None:
    """UT02-57 a filter error becomes ConfigError naming the file, class and line."""
    file = _sql_file(tmp_path, "100_stg.sql", "SELECT\n{{ 'Bad-Name' | ident }};\n")
    with pytest.raises(
        ConfigError, match=r"^render failed for 100_stg\.sql: ConfigError at line 2$"
    ):
        render_sql(file, _context(tmp_path))


def test_ut02_57_macros_and_raw(tmp_path: Path) -> None:
    """UT02-57 `_macros.jinja` is importable and `raw` is bound to the context's lake."""
    sql_dir = tmp_path / "sql"
    sql_dir.mkdir()
    (sql_dir / "_macros.jinja").write_text(
        "{% macro col(c) %}{{ raw('servicenow', 'incident', c, 'VARCHAR') }}{% endmacro %}",
        encoding="utf-8",
    )
    file = _sql_file(
        tmp_path,
        "100_stg.sql",
        "{% import '_macros.jinja' as m %}\nSELECT {{ m.col('number') }}, {{ m.col('gone') }};\n",
    )
    out = render_sql(file, _context(tmp_path, frozenset({"number"})))
    assert out == 'SELECT "number", CAST(NULL AS VARCHAR);\n'
    out = render_sql(file, _context(tmp_path, frozenset({"gone"})))
    assert out == 'SELECT CAST(NULL AS VARCHAR), "gone";\n'


def test_ut02_57_template_vars_keys(tmp_path: Path) -> None:
    """UT02-57 template_vars returns exactly the context fields plus raw_root."""
    ctx = _context(tmp_path / "raw")
    tv = ctx.template_vars()
    assert set(tv) == {"build_id", "dq", "custom_fields", "lake", "extra_entities", "raw_root"}
    assert tv["raw_root"] == (tmp_path / "raw").as_posix()
    assert tv["lake"] is ctx.lake


def test_ut02_57_build_render_context(tmp_path: Path) -> None:
    """UT02-57 build_render_context copies dq, custom fields and configured entity names."""
    dq = DqSettings(row_count_drop_max=0.1)
    custom = CustomFieldsConfig.model_validate({"jira": {"story_points": "customfield_10016"}})
    sections = SimpleNamespace(
        servicenow=SimpleNamespace(entities={"incident": object()}),
        files=SimpleNamespace(entities={"budget": object(), "headcount": object()}),
        mongodb=None,
        snowflake=SimpleNamespace(entities={"cost": object()}),
        dataverse=None,
    )
    cfg = SimpleNamespace(
        sources=SimpleNamespace(dq=dq, sources=sections),
        mappings=SimpleNamespace(custom_fields=custom),
    )
    inv = _inventory(tmp_path)
    ctx = build_render_context(cfg, inv, BUILD_ID)
    assert ctx.build_id == BUILD_ID
    assert ctx.dq is dq
    assert ctx.custom_fields is custom
    assert ctx.lake is inv
    assert dict(ctx.extra_entities) == {"files": ("budget", "headcount"), "snowflake": ("cost",)}
    with pytest.raises(TypeError):
        ctx.extra_entities["x"] = ()  # type: ignore[index]
    with pytest.raises(ConfigError, match="invalid build id"):
        build_render_context(cfg, inv, "not-a-build")


# --- UT02-58 filters --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [("incident", '"incident"'), ("_x9", '"_x9"'), ("a" * 128, '"' + "a" * 128 + '"')],
)
def test_ut02_58_ident_ok(value: str, expected: str) -> None:
    """UT02-58 ident quotes allowlisted names."""
    assert sqlfiles._ident(value) == expected


@pytest.mark.parametrize("value", ["", "A", "9a", "a-b", 'a"b', "a" * 129, "a b", 5, None])
def test_ut02_58_ident_bad(value: object) -> None:
    """UT02-58 ident rejects anything outside the pattern."""
    with pytest.raises(ConfigError, match="invalid identifier"):
        sqlfiles._ident(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("abc", "'abc'"), ("it's", "'it''s'"), ("", "''"), ("x" * 1024, "'" + "x" * 1024 + "'")],
)
def test_ut02_58_sqlstr_ok(value: str, expected: str) -> None:
    """UT02-58 sqlstr quotes and doubles single quotes."""
    assert sqlfiles._sqlstr(value) == expected


@pytest.mark.parametrize("value", ["x" * 1025, "a\nb", "\x00", "tab\t", 3, None])
def test_ut02_58_sqlstr_bad(value: object) -> None:
    """UT02-58 sqlstr rejects long, control-character and non-string values."""
    with pytest.raises(ConfigError, match="invalid SQL string literal"):
        sqlfiles._sqlstr(value)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (5, "5"),
        (-3, "(-3)"),
        (0.05, "0.05"),
        (-0.5, "(-0.5)"),
        (1e-7, "1e-07"),
        (Decimal("1.50"), "1.50"),
        (Decimal("-2"), "(-2)"),
    ],
)
def test_ut02_58_num_ok(value: object, expected: str) -> None:
    """UT02-58 num renders int, float and Decimal."""
    assert sqlfiles._num(value) == expected


@pytest.mark.parametrize(
    "value", [True, False, math.inf, -math.inf, math.nan, Decimal("NaN"), Decimal("Infinity"), "1"]
)
def test_ut02_58_num_bad(value: object) -> None:
    """UT02-58 num rejects bools, non-finite numbers and strings."""
    with pytest.raises(ConfigError, match="invalid number"):
        sqlfiles._num(value)


def test_ut02_58_sqldate() -> None:
    """UT02-58 sqldate renders a date and rejects other types."""
    assert sqlfiles._sqldate(datetime.date(2026, 9, 1)) == "DATE '2026-09-01'"
    for bad in (datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC), "2026-09-01", 1):
        with pytest.raises(ConfigError):
            sqlfiles._sqldate(bad)


def test_ut02_58_raw(tmp_path: Path) -> None:
    """UT02-58 raw returns the quoted column when present, else a typed NULL."""
    inv = _inventory(tmp_path, frozenset({"number", "_payload"}))
    assert sqlfiles._raw(inv, "servicenow", "incident", "number", "VARCHAR") == '"number"'
    for sqltype in ("VARCHAR", "BOOLEAN", "DOUBLE", "BIGINT", "TIMESTAMPTZ"):
        assert (
            sqlfiles._raw(inv, "servicenow", "incident", "gone", sqltype)
            == f"CAST(NULL AS {sqltype})"
        )
    assert sqlfiles._raw(inv, "jira", "issue", "number", "BIGINT") == "CAST(NULL AS BIGINT)"
    with pytest.raises(ConfigError):
        sqlfiles._raw(inv, "servicenow", "incident", "Bad", "VARCHAR")
    with pytest.raises(ConfigError):
        sqlfiles._raw(inv, "servicenow", "incident", "number", "TEXT; DROP")
    with pytest.raises(ConfigError):
        sqlfiles._raw(inv, "Bad", "incident", "number", "VARCHAR")


# --- ST02-11 sandbox --------------------------------------------------------------------


@pytest.mark.parametrize(
    "body",
    [
        "{{ ''.__class__.__mro__ }}",
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ dq.__class__ }}",
        "{{ lake.get.__globals__ }}",
    ],
)
def test_st02_11_sandbox_escape(tmp_path: Path, body: str) -> None:
    """ST02-11 template code reaching for dunder attributes raises ConfigError (SecurityError)."""
    file = _sql_file(tmp_path, "100_stg.sql", body + "\n")
    with pytest.raises(ConfigError, match=r"SecurityError at line 1$"):
        render_sql(file, _context(tmp_path))


@pytest.mark.parametrize(
    "body",
    [
        "{{ lake.root }}",
        "{{ lake.root.read_text() }}",
        "{{ lake.root.joinpath('pwned.txt').write_text('owned') }}",
        "{{ lake.root.parent.joinpath('x').unlink() }}",
        "{{ dq.model_dump() }}",
        "{{ build_id.upper() }}",
        "{{ lake.get('servicenow', 'incident').columns.union }}",
    ],
)
def test_st02_11_non_dunder_escape(tmp_path: Path, body: str) -> None:
    """ST02-11 public methods outside the allowlist (Path I/O included) are SecurityError."""
    file = _sql_file(tmp_path, "100_stg.sql", body + "\n")
    with pytest.raises(ConfigError, match=r"SecurityError at line 1$"):
        render_sql(file, _context(tmp_path / "raw"))
    assert not (tmp_path / "raw").exists()
    assert not (tmp_path / "pwned.txt").exists()


def test_st02_11_allowlisted_reads(tmp_path: Path) -> None:
    """ST02-11 the allowlist keeps mapping reads, entity fields, loops and namespaces working."""
    body = (
        "{% set ns = namespace(n=0) %}\n"
        "{% for key, ent in lake.entities.items() %}\n"
        "{% set ns.n = ns.n + ent.files %}\n"
        "{{ loop.index }} {{ key[1] }} {{ ent.present }} {{ custom_fields.jira.team is none }}\n"
        "{% endfor %}\n"
        "{{ ns.n }} {{ extra_entities.get('files') | join(',') }} {{ lake.from_synth }}\n"
    )
    file = _sql_file(tmp_path, "100_stg.sql", body)
    out = render_sql(file, _context(tmp_path, frozenset({"number"})))
    assert out == "1 incident True True\n1 budget False\n"


@pytest.mark.parametrize(
    ("body", "cls"),
    [
        ("{{ raw('a') }}", "TypeError"),
        ("{{ 1 // 0 }}", "ZeroDivisionError"),
        ("{{ range(10 ** 9) | list }}", "OverflowError"),
    ],
)
def test_ut02_57_runtime_errors_mapped(tmp_path: Path, body: str, cls: str) -> None:
    """UT02-57 runtime failures inside a render become ConfigError naming the class and line."""
    file = _sql_file(tmp_path, "100_stg.sql", "SELECT 1;\n" + body + "\n")
    with pytest.raises(ConfigError, match=rf"^render failed for 100_stg\.sql: {cls} at line 2$"):
        render_sql(file, _context(tmp_path))


def test_st02_11_include_outside_dir(tmp_path: Path) -> None:
    """ST02-11 templates load only from the SQL directory: a path escape is refused."""
    (tmp_path / "secret.sql").write_text("leak", encoding="utf-8")
    file = _sql_file(tmp_path, "100_stg.sql", "{% include '../secret.sql' %}\n")
    with pytest.raises(ConfigError, match="TemplateNotFound"):
        render_sql(file, _context(tmp_path))
