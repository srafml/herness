"""Unit tests for the warehouse tools, part 1 (impl 05 U05-39, U05-40, U05-41, U05-43).

UT05-80 `list_tables`, UT05-81 `describe_table`, UT05-82 `run_sql`, UT05-84 `get_scores`, on
the stand-in build of `tests.support.warehouse_tools_build` (re-point to `tiny_build` when
spec 11 lands).
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from pydantic import JsonValue
from tests.support import tools_standin as sd
from tests.support import warehouse_tools_build as wb
from tests.support.dispatch_standin import call, make_state, use_test_config
from tests.support.harness_fakes import FakeOps

from herness.core import config as c
from herness.core.errors import ConfigError, QueryError, ToolInputError
from herness.core.resilience import ProcessState
from herness.core.types import SqlLimits, ToolCall, ToolContext, ToolResult
from herness.harness import _warehouse_tools_sql as ws
from herness.harness import warehouse_tools as wt
from herness.harness.tools import RecordedResult, ToolRegistry, dispatch, tool_registry
from herness.harness.warehouse import DuckWarehouse

pytestmark = pytest.mark.unit

UNTRUSTED_OPEN = '<untrusted_data source="warehouse" record_id="">'


@pytest.fixture
def cfg(tmp_path: Path, reset_process_state: ProcessState) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path / "cfg")
    yield
    c.reset_config()


@pytest.fixture
def wh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: None) -> Iterator[DuckWarehouse]:
    del cfg
    wb.patch_redaction(monkeypatch)
    with wb.opened(tmp_path / "wh") as handle:
        yield handle


def _ctx(wh: DuckWarehouse, *, limits: SqlLimits | None = None) -> ToolContext:
    return sd.make_ctx(wh, FakeOps(), limits=limits)


def _ops(ctx: ToolContext) -> FakeOps:
    assert isinstance(ctx.ops, FakeOps)
    return ctx.ops


def _rows(result: ToolResult) -> list[dict[str, Any]]:
    assert result.data is not None
    data: Any = result.data
    return [dict(zip(data["columns"], row, strict=True)) for row in data["rows"]]


def _dispatch(ctx: ToolContext, calls: list[ToolCall]) -> list[ToolResult]:
    tools = {t.name: t for t in wt._TOOLS}
    return asyncio.run(dispatch(ctx, tools, calls, None, make_state()))


# --- registration ----------------------------------------------------------------------------


def test_ut05_80_register_warehouse_tools_owner_05_idempotent(cfg: None) -> None:
    """UT05-80 the four part-1 tools register with owner 05 (strict schemas), idempotently."""
    del cfg
    wt.register_warehouse_tools()
    wt.register_warehouse_tools()
    assert tool_registry().names() == ["describe_table", "get_scores", "list_tables", "run_sql"]
    other = ToolRegistry()
    wt.register_warehouse_tools(other)
    assert other.names() == tool_registry().names()
    for spec in other.tool_specs(list(wt._TOOLS)):
        assert spec.strict
        assert len(spec.description) <= 1_024


# --- UT05-80 list_tables ---------------------------------------------------------------------


def test_ut05_80_list_tables_all_schemas_only_allowed(wh: DuckWarehouse) -> None:
    """UT05-80 list_tables(null): only allowed-schema tables, query_id header, one recorded
    query, `data.tables`; tables in main, secret and stg never appear."""
    ctx = _ctx(wh)
    result = wt.ListTables()(ctx, schema=None)
    assert result.ok
    lines = result.content.splitlines()
    (qid,) = result.query_ids
    assert lines[0] == f"query_id={qid} tables={len(lines) - 1}"
    listed = [line.split(" | ")[0] for line in lines[1:]]
    assert listed == sorted(listed)
    assert {name.split(".")[0] for name in listed} == {"core", "enrich", "metrics", "score", "meta"}
    for hidden in (*wb.HIDDEN_TABLES, "credentials", "hidden_main", "raw_payload", "secret"):
        assert hidden not in result.content
    assert "core.incident | rows=40 | Incidents, one row per ticket" in lines
    assert result.data is not None
    tables: Any = result.data["tables"]
    assert [t["table"] for t in tables] == listed
    assert {
        "table": "core.incident",
        "rows": 40,
        "description": "Incidents, one row per ticket",
    } in (tables)
    assert list(_ops(ctx).evidence) == [qid]
    assert [use[0] for use in _ops(ctx).uses] == [qid]


def test_ut05_80_list_tables_one_schema_and_comment_cut(wh: DuckWarehouse) -> None:
    """UT05-80 list_tables(meta): only that schema; the comment is cut to 120 chars on one
    line with `|` escaped; a second call is served from the result cache (same query_id)."""
    ctx = _ctx(wh)
    first = wt.ListTables()(ctx, schema="meta")
    lines = first.content.splitlines()
    assert lines[1:] == [f"meta.build_info | rows=2 | {wb.LONG_COMMENT[:120].replace('|', '\\|')}"]
    again = wt.ListTables()(ctx, schema="meta")
    assert again.query_ids == first.query_ids
    other = wt.ListTables()(ctx, schema="score")
    assert other.query_ids != first.query_ids
    assert all(line.startswith("score.") for line in other.content.splitlines()[1:])


def test_ut05_80_list_tables_schema_argument_validated(wh: DuckWarehouse) -> None:
    """UT05-80 through dispatch, a schema outside the enum is refused before execution."""
    ctx = _ctx(wh)
    (result,) = _dispatch(ctx, [call("list_tables", "c1", schema="secret")])
    assert not result.ok
    assert result.error is not None
    assert result.error.type == "ToolInputError"
    assert _ops(ctx).evidence == {}


# --- UT05-81 describe_table ------------------------------------------------------------------


def test_ut05_81_describe_incident_marks_blocked_samples_without_them(wh: DuckWarehouse) -> None:
    """UT05-81 describe core.incident: every configured blocked column is marked BLOCKED; the
    5 sample rows never contain a blocked column; two recorded queries."""
    ctx = _ctx(wh)
    result = wt.DescribeTable()(ctx, table="core.incident")
    assert result.ok
    cols_qid, sample_qid = result.query_ids
    head, _, sample_text = result.content.partition("\nsample rows:\n")
    lines = head.splitlines()
    assert lines[0] == f"query_id={cols_qid} table=core.incident columns=9"
    blocked = sorted(c.split(".")[2] for c in wb.BLOCKED if c.startswith("core.incident."))
    assert blocked == ["close_notes", "description", "short_description"]
    for name in blocked:
        assert f"{name} | VARCHAR | yes | {wt.BLOCKED_MARK}" in lines
    assert "service_id | VARCHAR | yes | owning service \\| from the CMDB" in lines
    assert sample_text.startswith(f"query_id={sample_qid} rows=5 shown=5")
    assert "raw " not in sample_text  # no blocked cell value
    for name in blocked:
        assert name not in sample_text
    data: Any = result.data
    assert [col["name"] for col in data["columns"] if col["blocked"]] == [
        "short_description",
        "description",
        "close_notes",
    ]
    assert not set(blocked) & set(data["sample"]["columns"])
    assert len(data["sample"]["rows"]) == 5
    evidence = _ops(ctx).evidence
    assert set(evidence) == {cols_qid, sample_qid}
    assert all(not set(blocked) & set(row) for row in evidence[sample_qid].result_sample)


def test_ut05_81_describe_redacts_redact_on_read_and_wraps(wh: DuckWarehouse) -> None:
    """UT05-81 describe core.work_item: the redact-on-read `summary` cells are redacted in
    content, data and evidence, and wrapped as untrusted."""
    ctx = _ctx(wh)
    result = wt.DescribeTable()(ctx, table="core.work_item")
    assert wb.PERSONAL not in result.content
    assert "[EMAIL]" in result.content
    assert UNTRUSTED_OPEN in result.content
    assert wb.PERSONAL not in repr(result.data)
    for ev in _ops(ctx).evidence.values():
        assert wb.PERSONAL not in repr(ev.result_sample)


@pytest.mark.parametrize("table", ["core.incidnt", "core.credentials", "score.fundng"])
def test_ut05_81_unknown_table_hint_names_only_allowed_tables(
    wh: DuckWarehouse, table: str
) -> None:
    """UT05-81 an unknown table: ToolInputError with the 3 closest allowed names; a name in a
    non-allowed schema is never suggested."""
    ctx = _ctx(wh)
    with pytest.raises(ToolInputError, match=f"table {table} does not exist") as info:
        wt.DescribeTable()(ctx, table=table)
    hint = info.value.hint
    assert hint is not None
    assert hint.startswith("closest: ")
    names = hint.removeprefix("closest: ").split(", ")
    assert len(names) == 3
    assert all(n.split(".")[0] in {"core", "enrich", "metrics", "score", "meta"} for n in names)
    assert "credentials" not in hint
    assert _ops(ctx).evidence == {}


def test_ut05_81_non_allowed_schema_is_unknown_even_on_direct_call(wh: DuckWarehouse) -> None:
    """UT05-81 a direct call naming `secret.credentials` or `stg.raw_payload` (dispatch would
    refuse the pattern) is an unknown table; nothing runs."""
    ctx = _ctx(wh)
    for table in ("secret.credentials", "stg.raw_payload", "main.hidden_main"):
        with pytest.raises(ToolInputError):
            wt.DescribeTable()(ctx, table=table)
    (result,) = _dispatch(ctx, [call("describe_table", "c1", table="secret.credentials")])
    assert not result.ok
    assert _ops(ctx).evidence == {}


def test_ut05_81_all_blocked_table_has_no_sample(
    wh: DuckWarehouse, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT05-81 a table whose every column is blocked gets no sample query."""
    ctx = _ctx(wh)
    everything = frozenset({"enrich.text_redacted.record_id", "enrich.text_redacted.text"})
    monkeypatch.setattr(ws, "blocked_columns", lambda: everything)
    result = wt.DescribeTable()(ctx, table="enrich.text_redacted")
    assert len(result.query_ids) == 1
    assert "sample rows" not in result.content
    assert result.data is not None
    assert result.data["sample"] is None


def test_ut05_81_fullwidth_lookalike_blocked_column(wh: DuckWarehouse) -> None:
    """UT05-81 a blocked column spelled with fullwidth lookalike letters
    (`core.change.description`) is marked BLOCKED and left out of the sample, matched under
    the guard's NFKC + casefold normalisation; the call succeeds (no internal guard error)."""
    ctx = _ctx(wh)
    result = wt.DescribeTable()(ctx, table="core.change")
    assert result.ok
    data: Any = result.data
    marks = {col["name"]: col["blocked"] for col in data["columns"]}
    assert marks == {"record_id": False, wb.LOOKALIKE_BLOCKED: True, "state": False}
    assert f"{wb.LOOKALIKE_BLOCKED} | VARCHAR | yes | {wt.BLOCKED_MARK}" in result.content
    assert data["sample"]["columns"] == ["record_id", "state"]
    assert "raw change" not in result.content
    assert "raw change" not in repr(data)
    assert len(result.query_ids) == 2


def test_ut05_81_concurrent_tools_on_cold_warehouse(wh: DuckWarehouse) -> None:
    """UT05-81 six concurrent describe_table / list_tables calls through dispatch on a cold
    warehouse (schema not loaded) all succeed (T05-16 own-cursor carry-over)."""
    ctx = _ctx(wh)
    tables = ["core.incident", "core.work_item", "score.funding", "metrics.incident_monthly"]
    calls = [call("describe_table", f"d{i}", table=t) for i, t in enumerate(tables)]
    calls += [call("list_tables", "l1", schema=None), call("list_tables", "l2", schema="core")]
    results = _dispatch(ctx, calls)
    assert all(r.ok for r in results), [r.content for r in results]
    assert results[4].content.count("\n") > results[5].content.count("\n")


# --- UT05-82 run_sql -------------------------------------------------------------------------


def test_ut05_82_run_sql_aggregate_data_and_content(wh: DuckWarehouse) -> None:
    """UT05-82 an aggregate: data {columns, rows, row_count, truncated}, content table with
    the query_id header, query_ids = [qid], evidence recorded."""
    ctx = _ctx(wh)
    sql = (
        "SELECT service_id, count(*) AS n, sum(mttr_hours) AS hours FROM core.incident"
        " GROUP BY service_id ORDER BY service_id"
    )
    result = wt.RunSql()(ctx, sql=sql, purpose="incidents per service")
    assert result.ok
    (qid,) = result.query_ids
    assert result.row_count == 4
    assert not result.truncated
    assert result.data is not None
    assert result.data["columns"] == ["service_id", "n", "hours"]
    assert result.data["row_count"] == 4
    assert result.data["truncated"] is False
    rows = _rows(result)
    assert [r["service_id"] for r in rows] == ["svc_0", "svc_1", "svc_2", "svc_3"]
    assert sum(r["n"] for r in rows) == 40
    assert isinstance(rows[0]["hours"], str)  # Decimal is JSON-safe as text
    lines = result.content.splitlines()
    assert lines[0] == f"query_id={qid} rows=4 shown=4 truncated=no ordered=yes"
    assert lines[1] == "service_id | n | hours"
    assert lines[3].startswith("svc_0 | 10 | ")
    assert set(_ops(ctx).evidence) == {qid}


def test_ut05_82_run_sql_untrusted_text_wrapped_and_redacted(wh: DuckWarehouse) -> None:
    """UT05-82 an untrusted text query (`core.work_item.summary`): cells wrapped in the R-20
    block (injection cannot close it) and redacted in content and data."""
    ctx = _ctx(wh)
    sql = "SELECT record_id, summary AS s FROM core.work_item ORDER BY record_id LIMIT 6"
    result = wt.RunSql()(ctx, sql=sql, purpose="look at summaries")
    assert result.content.count(UNTRUSTED_OPEN) == 6
    assert "&lt;/untrusted_data&gt;" in result.content
    assert wb.PERSONAL not in result.content
    assert "[EMAIL]" in result.content
    summaries = [r["s"] for r in _rows(result)]
    assert wb.PERSONAL not in repr(summaries)
    assert any("[EMAIL]" in s for s in summaries)


def test_ut05_82_run_sql_truncated_and_guarded(wh: DuckWarehouse) -> None:
    """UT05-82 more rows than `return_rows`: truncated; agent SQL always runs guarded, so a
    blocked column or a write is a QueryError and records nothing."""
    ctx = _ctx(wh, limits=SqlLimits(timeout_s=30.0, return_rows=5))
    result = wt.RunSql()(ctx, sql="SELECT record_id FROM core.incident", purpose="p")
    assert result.row_count == 40
    assert result.truncated
    assert result.data is not None
    assert result.data["truncated"] is True
    assert len(result.data["rows"]) == 5  # type: ignore[arg-type]
    before = dict(_ops(ctx).evidence)
    for bad in ("SELECT short_description FROM core.incident", "SELECT * FROM core.incident"):
        with pytest.raises(QueryError) as info:
            wt.RunSql()(ctx, sql=bad, purpose="p")
        assert info.value.hint is not None
        assert info.value.hint.startswith("free text is not available")
    with pytest.raises(QueryError):
        wt.RunSql()(ctx, sql="DELETE FROM core.incident", purpose="p")
    with pytest.raises(QueryError):
        wt.RunSql()(ctx, sql="SELECT a FROM secret.credentials", purpose="p")
    assert _ops(ctx).evidence == before


def test_ut05_82_run_sql_requires_purpose(wh: DuckWarehouse) -> None:
    """UT05-82 `purpose` is required by the schema and by a direct call."""
    ctx = _ctx(wh)
    with pytest.raises(ToolInputError, match="missing argument purpose"):
        wt.RunSql()(ctx, sql="SELECT 1 AS a")
    (result,) = _dispatch(ctx, [call("run_sql", "c1", sql="SELECT 1 AS a")])
    assert not result.ok
    with pytest.raises(ToolInputError, match="sql must be a string"):
        wt.RunSql()(ctx, sql=None, purpose="p")


def test_ut05_82_redact_on_read_non_text_cell_redacted_as_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT05-82 a non-text cell of a redact-on-read output (e.g. a list) is redacted as its
    JSON text; NULL stays NULL; other columns are untouched."""
    wb.patch_redaction(monkeypatch)
    result = RecordedResult(
        query_id="q_0123456789abcdef",
        columns=["titles", "n"],
        types=["VARCHAR[]", "INTEGER"],
        rows=[([wb.PERSONAL], 1), (None, 2)],
        row_count=2,
        truncated=False,
        ordered=True,
        redact_columns=frozenset({"titles"}),
    )
    assert ws.redacted(result).rows == [('["[EMAIL]"]', 1), (None, 2)]


# --- UT05-84 get_scores ----------------------------------------------------------------------


def _sorted_by(rows: list[dict[str, Any]], *keys: str, desc: str = "") -> bool:
    def key(row: Mapping[str, Any]) -> tuple[Any, ...]:
        parts: list[Any] = []
        for k in keys:
            value = row[k]
            if k == desc:
                value = -float(value)
            elif k == "order_rank":
                value = (value is None, value or 0)
            parts.append(value)
        return tuple(parts)

    return [key(r) for r in rows] == sorted(key(r) for r in rows)


def _scores(ctx: ToolContext, kind: str, **kw: JsonValue) -> ToolResult:
    args: dict[str, JsonValue] = {"entity_id": None, "top": 100, "scenario": None, **kw}
    return wt.GetScores()(ctx, kind=kind, **args)


def test_ut05_84_each_kind_ordered_as_specified(wh: DuckWarehouse) -> None:
    """UT05-84 each kind returns its rows in the specified order with stored query_ids."""
    ctx = _ctx(wh)
    funding = _rows(_scores(ctx, "funding"))
    assert len(funding) == wb.FUNDING_ROWS
    ranks = [r["rank"] for r in funding]
    assert len(set(ranks)) < len(ranks)  # ties on rank: candidate_id breaks them
    assert _sorted_by(funding, "rank", "candidate_id")
    assert all(r["query_ids"][0].startswith("q_") for r in funding)
    org = _rows(_scores(ctx, "org"))
    assert len(org) == 8
    assert _sorted_by(org, "rank", "entity_id", "metric")
    lever = _rows(_scores(ctx, "action_lever"))
    assert len(lever) == wb.LEVER_ROWS
    assert _sorted_by(lever, "delta_usd", "entity_id", "metric", desc="delta_usd")
    pairs = [(r["delta_usd"], r["entity_id"]) for r in lever]
    assert len({r["delta_usd"] for r in lever}) < len(lever)  # ties on delta_usd
    assert len(set(pairs)) < len(pairs)  # and on (delta_usd, entity_id): metric decides
    portfolio = _rows(_scores(ctx, "portfolio"))
    assert len(portfolio) == 10
    assert _sorted_by(portfolio, "scenario", "order_rank", "candidate_id")
    assert portfolio[-1]["order_rank"] is None
    assert list(_rows(_scores(ctx, "org"))[0]) == [
        "entity_type", "entity_id", "metric", "value", "peer_group", "peer_median", "z_score",
        "trend_slope", "sample_size", "composite", "rank", "unconfirmed", "flags", "query_ids",
    ]  # fmt: skip


def test_ut05_84_filters_top_and_scenario(wh: DuckWarehouse) -> None:
    """UT05-84 `entity_id`, `top` and (portfolio) `scenario` filter; one recorded query."""
    ctx = _ctx(wh)
    top3 = _scores(ctx, "funding", top=3)
    assert [r["rank"] for r in _rows(top3)] == [1, 1, 2]
    assert len(top3.query_ids) == 1
    one = _rows(_scores(ctx, "funding", entity_id="cand_05"))
    assert [r["candidate_id"] for r in one] == ["cand_05"]
    team = _rows(_scores(ctx, "org", entity_id="team_1"))
    assert {r["entity_id"] for r in team} == {"team_1"}
    lean = _rows(_scores(ctx, "portfolio", scenario="lean"))
    assert {r["scenario"] for r in lean} == {"lean"}
    assert len(lean) == 5


def test_ut05_84_funding_title_redacted_and_wrapped(wh: DuckWarehouse) -> None:
    """UT05-84 `score.funding.title` is redact-on-read (D9) and untrusted."""
    ctx = _ctx(wh)
    result = _scores(ctx, "funding", top=2)
    assert wb.PERSONAL not in result.content
    assert wb.PERSONAL not in repr(result.data)
    assert result.content.count(UNTRUSTED_OPEN) == 2
    assert all(r["title"].startswith("Fix [EMAIL] login") for r in _rows(result))


@pytest.mark.parametrize("kind", ["funding", "org", "action_lever"])
def test_ut05_84_scenario_with_non_portfolio_is_tool_input_error(
    wh: DuckWarehouse, kind: str
) -> None:
    """UT05-84 a scenario with a kind other than portfolio: ToolInputError, nothing runs."""
    ctx = _ctx(wh)
    with pytest.raises(ToolInputError, match="scenario applies only to kind portfolio"):
        _scores(ctx, kind, scenario="base")
    assert _ops(ctx).evidence == {}


def test_ut05_84_bad_arguments_on_direct_call(wh: DuckWarehouse) -> None:
    """UT05-84 direct calls: unknown kind and a non-integer top are ToolInputError; a score
    table missing from the build is a QueryError (never a fatal internal-SQL ConfigError)."""
    ctx = _ctx(wh)
    with pytest.raises(ToolInputError, match="kind must be"):
        _scores(ctx, "bogus")
    with pytest.raises(ToolInputError, match="top must be an integer"):
        _scores(ctx, "org", top=True)
    with pytest.raises(ToolInputError, match="entity_id must be a string or null"):
        _scores(ctx, "org", entity_id=3)
    wh._schema = {"score": {}}  # type: ignore[assignment]
    with pytest.raises(QueryError, match="not in this build"):
        _scores(ctx, "org")
    assert not isinstance(QueryError("x"), ConfigError)
