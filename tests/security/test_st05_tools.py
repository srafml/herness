"""Security tests for the tool registry and dispatch (impl 05 U05-33, U05-34).

ST05-02 (tool outside the allow-list), ST05-07 (excessive agency), ST05-10 (tool-call
flooding), ST05-16 (malformed or oversized arguments), ST05-22 (task tool injection).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path

import duckdb
import pytest
from pydantic import JsonValue
from tests.support import tools_standin as sd
from tests.support.dispatch_standin import (
    SyncTool,
    call,
    make_state,
    make_tool_ctx,
    ok_result,
    strict_schema,
    use_test_config,
)
from tests.support.harness_fakes import FakeOps, RecordingTracer

from herness.core import config as c
from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState
from herness.core.types import AsyncTool, LoopState, Tool, ToolCall, ToolContext, ToolResult
from herness.harness import _tools_schema
from herness.harness import tools as tools_mod
from herness.harness.tools import (
    MAX_TOOL_ARGUMENT_CHARS,
    TOOL_CONTENT_MAX_CHARS,
    TOOL_OWNERS,
    ToolRegistry,
    dispatch,
    execute_recorded,
    format_result,
    tool_registry,
)
from herness.harness.warehouse import DuckWarehouse

pytestmark = pytest.mark.unit

SPEC_05_TOOLS = {
    "list_tables",
    "describe_table",
    "run_sql",
    "get_metric",
    "get_scores",
    "get_cluster",
    "get_record",
    "semantic_search",
}
FORBIDDEN_WORDS = ("url", "http", "fetch", "email", "mail", "shell", "exec", "write", "delete")
N_SCHEMA = strict_schema({"n": {"type": "integer"}})


@dataclass(frozen=True)
class Role:
    """Minimal stand-in for `RoleSpec` (T05-19 not built yet)."""

    name: str
    allowed_tools: frozenset[str]


ANALYST = Role("analyst", frozenset({"run_sql", "list_tables", "get_metric"}))


@pytest.fixture
def cfg(tmp_path: Path, reset_process_state: ProcessState) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path)
    yield
    c.reset_config()


def _run(
    ctx: ToolContext,
    tools: Mapping[str, Tool | AsyncTool],
    calls: list[ToolCall],
    state: LoopState | None = None,
) -> list[ToolResult]:
    return asyncio.run(dispatch(ctx, tools, calls, None, state or make_state()))


# --- ST05-02 ---------------------------------------------------------------------------------


def test_st05_02_escalate_and_made_up_shell_never_execute(cfg: None) -> None:
    """ST05-02 a scripted analyst calls `escalate` and a made-up `shell`: ToolInputError
    results, nothing executed; `resolve` refuses `escalate` for the analyst role."""
    del cfg
    reg = ToolRegistry()
    spies = {name: SyncTool(name) for name in ("run_sql", "list_tables", "escalate")}
    reg.register(spies["run_sql"], owner="05")
    reg.register(spies["list_tables"], owner="05")
    reg.register(spies["escalate"], owner="06")
    with pytest.raises(ConfigError, match="escalate not allowed for role analyst"):
        reg.resolve(ANALYST, ["run_sql", "escalate"], {})
    with pytest.raises(ConfigError, match="shell"):
        reg.register(SyncTool("shell"), owner="05")
    resolved = {t.name: t for t in reg.resolve(ANALYST, ["run_sql", "list_tables"], {})}
    calls = [call("escalate", "e1"), call("shell", "s1"), call("execute_recorded", "x1")]
    results = _run(make_tool_ctx(), resolved, calls)
    for result in results:
        assert not result.ok
        assert result.error is not None
        assert result.error.type == "ToolInputError"
        assert result.error.message.endswith("not allowed; allowed: list_tables, run_sql")
    assert all(spy.calls == [] for spy in spies.values())


# --- ST05-07 ---------------------------------------------------------------------------------


def test_st05_07_only_tool_owners_names_can_exist() -> None:
    """ST05-07 static: the owner table holds exactly the 14 designed tools, none of them a
    URL, email, shell or write tool; the registry refuses every other name for every owner."""
    assert set(TOOL_OWNERS) == SPEC_05_TOOLS | {
        "recall_memory",
        "propose_memory",
        "post_finding",
        "list_findings",
        "request_subtask",
        "escalate",
    }
    assert {n for n, o in TOOL_OWNERS.items() if o == "05"} == SPEC_05_TOOLS
    for name in TOOL_OWNERS:
        assert not any(word in name for word in FORBIDDEN_WORDS), name
    reg = tool_registry()
    for name in ("shell", "fetch_url", "send_email", "write_file", "http_get", "run_python"):
        for owner in ("05", "06", "07"):
            with pytest.raises(ConfigError):
                reg.register(SyncTool(name), owner=owner)  # type: ignore[arg-type]
    assert set(reg.names()) <= set(TOOL_OWNERS)
    assert not hasattr(tools_mod, "register_tool_by_path")  # no dynamic tool loading hook


class _SpyCursor:
    """Records every statement sent to DuckDB, then delegates (spy cursor of ST05-07)."""

    def __init__(self, inner: duckdb.DuckDBPyConnection, seen: list[str]) -> None:
        self._inner, self._seen = inner, seen

    def execute(self, sql: str, params: object = None) -> duckdb.DuckDBPyConnection:
        self._seen.append(sql)
        return self._inner.execute(sql, params)

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)


class _RunSqlStandin:
    """Stand-in for the spec 05 `run_sql` tool (T05-17 not built): `execute_recorded` only."""

    name = "run_sql"
    description = "Run one read-only SQL query"
    input_schema: dict[str, JsonValue] = strict_schema({"sql": {"type": "string"}})

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        sql = kwargs["sql"]
        assert isinstance(sql, str)
        content, _shown = format_result(execute_recorded(ctx, sql, {}))
        return ok_result(content)


@pytest.fixture
def spied_wh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cfg: None
) -> Iterator[tuple[DuckWarehouse, list[str]]]:
    del cfg
    sd.patch_config(monkeypatch)
    seen: list[str] = []
    with sd.opened(tmp_path / "wh") as wh:
        real = wh.cursor
        monkeypatch.setattr(wh, "cursor", lambda: _SpyCursor(real(), seen))
        yield wh, seen


def test_st05_07_spec05_sql_executes_only_select(
    spied_wh: tuple[DuckWarehouse, list[str]],
) -> None:
    """ST05-07 through registry and dispatch, the spec 05 SQL path sends only SELECT to DuckDB;
    write, DDL, COPY and ATTACH attempts are error results and never reach the cursor."""
    wh, seen = spied_wh
    reg = tool_registry()
    reg.register(_RunSqlStandin(), owner="05")
    tools = {t.name: t for t in reg.resolve(ANALYST, ["run_sql"], {})}
    attacks = [
        "DELETE FROM core.big",
        "INSERT INTO core.big VALUES (1)",
        "CREATE TABLE core.x AS SELECT 1 AS a",
        "COPY core.big TO 'out.csv'",
        "ATTACH 'other.duckdb' AS o",
        "SELECT 1; DROP TABLE core.big",
    ]
    calls = [call("run_sql", f"a{i}", sql=sql) for i, sql in enumerate(attacks)]
    calls.append(call("run_sql", "ok", sql="SELECT n FROM core.big ORDER BY n LIMIT 2"))
    ctx = sd.make_ctx(wh, FakeOps())
    results = _run(ctx, tools, calls)
    assert all(not r.ok for r in results[:-1])
    assert results[-1].ok, results[-1].content
    assert seen, "the valid query must reach the spy cursor"
    for sql in seen:
        assert sql.lstrip().upper().startswith(("SELECT", "WITH")), sql


# --- ST05-10 ---------------------------------------------------------------------------------


def test_st05_10_flood_of_200_calls(cfg: None) -> None:
    """ST05-10 200 calls in one message: at most 16 run, never more than max_parallel (4) in
    flight, every call answered in order, content capped at TOOL_CONTENT_MAX_CHARS."""
    del cfg
    lock, live, peak = threading.Lock(), [0], [0]

    def big(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.01)
        with lock:
            live[0] -= 1
        return ok_result("x" * 50_000)

    tool = SyncTool("get_record", big, schema=N_SCHEMA)
    calls = [call("get_record", f"c{i}", n=i) for i in range(200)]
    ctx = make_tool_ctx()
    results = _run(ctx, {"get_record": tool}, calls)
    assert len(tool.calls) == 16
    assert peak[0] <= 4
    assert len(results) == 200
    assert [r.tool_call_id for r in results] == [f"c{i}" for i in range(200)]
    assert all(r.ok and len(r.content) <= TOOL_CONTENT_MAX_CHARS for r in results[:16])
    assert all(r.truncated for r in results[:16])
    assert all(not r.ok for r in results[16:])
    tracer = ctx.tracer
    assert isinstance(tracer, RecordingTracer)
    assert len(tracer.events) == 200


# --- ST05-16 ---------------------------------------------------------------------------------


def _nested(depth: int) -> JsonValue:
    value: JsonValue = []
    for _ in range(depth):
        value = [value]
    return value


def test_st05_16_malformed_and_oversized_arguments(
    cfg: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST05-16 1 MB arguments, wrong types, extra properties, nested arrays: ToolInputError
    results, nothing executed; the size cap applies before any schema validation."""
    del cfg
    validated: list[Mapping[str, JsonValue]] = []
    real_hint = _tools_schema.schema_hint

    def spy_hint(schema: Mapping[str, JsonValue], arguments: Mapping[str, JsonValue]) -> str | None:
        validated.append(arguments)
        return real_hint(schema, arguments)

    monkeypatch.setattr("herness.harness._tools_dispatch.schema_hint", spy_hint)
    schema = strict_schema({"n": {"type": "integer"}, "tags": {"type": "array"}})
    tool = SyncTool("get_metric", schema=schema)
    huge = "A" * 1_000_000
    calls = [
        ToolCall(
            id="raw",
            name="get_metric",
            arguments={"n": 1, "tags": [huge]},
            raw_arguments='{"n": 1, "tags": ["' + huge + '"]}',
        ),
        call("get_metric", "parsed", n=2, tags=["B" * (MAX_TOOL_ARGUMENT_CHARS + 1)]),
        call("get_metric", "type", n="1", tags=[]),
        call("get_metric", "extra", n=3, tags=[], shell="rm -rf /"),  # noqa: S604 - an attack payload, never run
        call("get_metric", "deep", n=4, tags=_nested(80)),
        call("get_metric", "nested", n=[[[5]]], tags=[]),
    ]
    ctx = make_tool_ctx()
    results = _run(ctx, {"get_metric": tool}, calls)
    messages = [r.error.message if r.error else None for r in results]
    assert messages == [
        "tool arguments too large",
        "tool arguments too large",
        "invalid arguments",
        "invalid arguments",
        "tool arguments are not valid JSON values",
        "invalid arguments",
    ]
    assert all(r.error is not None and r.error.type == "ToolInputError" for r in results)
    assert tool.calls == []
    assert len(validated) == 3  # only the three calls that passed the size and JSON checks
    assert all(len(r.content) < 1_000 for r in results)
    tracer = ctx.tracer
    assert isinstance(tracer, RecordingTracer)
    payloads = [fields["payload"] for _t, _s, fields in tracer.events]
    assert payloads[0] == payloads[1] == payloads[4] == {"args": {}}  # blobs never traced


# --- ST05-22 ---------------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["run_sql", "shell", "recall_memory"])
def test_st05_22_task_tool_injection_refused(name: str) -> None:
    """ST05-22 a task tool named `run_sql` (spec 05), `shell` (unknown) or a spec 07 tool:
    ConfigError, even when the role allows the name."""
    reg = ToolRegistry()
    reg.register(SyncTool("run_sql"), owner="05")
    role = Role("analyst", frozenset({"run_sql", "shell", "recall_memory"}))
    with pytest.raises(ConfigError, match="must be a spec 06 tool"):
        reg.resolve(role, ["run_sql"], {name: SyncTool(name)})


def test_st05_22_task_tool_key_must_match_its_name() -> None:
    """ST05-22 a spec 06 key carrying a tool with another name (e.g. `run_sql`) is refused."""
    role = Role("analyst", frozenset({"post_finding"}))
    with pytest.raises(ConfigError, match="must be a spec 06 tool"):
        ToolRegistry().resolve(role, ["post_finding"], {"post_finding": SyncTool("run_sql")})


def test_st05_07_concurrent_selects_on_cold_schema(
    spied_wh: tuple[DuckWarehouse, list[str]],
) -> None:
    """ST05-07 six concurrent SELECTs on a cold warehouse schema all succeed (regression: the
    shared-connection schema load raced under concurrent dispatch)."""
    wh, _seen = spied_wh
    tools = {"run_sql": _RunSqlStandin()}
    calls = [
        call("run_sql", f"q{i}", sql=f"SELECT n FROM core.big ORDER BY n LIMIT {i + 1}")  # noqa: S608 - fixed test SQL
        for i in range(6)
    ]
    results = _run(sd.make_ctx(wh, FakeOps()), tools, calls)
    assert all(r.ok for r in results), [r.content for r in results]
