"""Tests for herness.harness.tools.dispatch (impl 05 U05-34, flow F05-03).

UT05-70 - UT05-79. The full test config is cached (the `tool_store` retry policy and
`harness.tools.max_parallel` come from it); retry sleeps are no-ops (`reset_process_state`).
"""

from __future__ import annotations

import asyncio
import threading
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from pydantic import JsonValue
from tests.support.dispatch_standin import (
    AsyncFnTool,
    SyncTool,
    call,
    make_state,
    make_tool_ctx,
    ok_result,
    strict_schema,
    use_test_config,
)
from tests.support.harness_fakes import RecordingTracer

from herness.core import config as c
from herness.core.errors import (
    ConfigError,
    FatalError,
    PolicyViolation,
    QueryError,
    StoreBusy,
)
from herness.core.resilience import ProcessState
from herness.core.types import ToolCall, ToolContext, ToolResult
from herness.harness.tools import MAX_TOOL_CALLS_PER_MESSAGE, dispatch

pytestmark = pytest.mark.unit

N_SCHEMA = strict_schema({"n": {"type": "integer"}})
SQL_SCHEMA = strict_schema({"sql": {"type": "string"}})
STOP_HINT = "stop retrying this query; try get_metric or a simpler aggregate"


@pytest.fixture
def cfg(tmp_path: Path, reset_process_state: ProcessState) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path)
    yield
    c.reset_config()


def _run(ctx: ToolContext, tools: dict[str, object], calls: list[ToolCall], state=None):
    return asyncio.run(dispatch(ctx, tools, calls, None, state or make_state()))  # type: ignore[arg-type]


def _events(ctx: ToolContext) -> list[dict[str, object]]:
    tracer = ctx.tracer
    assert isinstance(tracer, RecordingTracer)
    return [fields for kind, _span, fields in tracer.events if kind == "tool_call"]


# --- UT05-70 ---------------------------------------------------------------------------------


def test_ut05_70_results_in_call_order_and_concurrent(cfg: None) -> None:
    """UT05-70 3 tools with different delays: call order kept, elapsed < sum of delays."""
    del cfg

    def sleeper(delay: float):
        def body(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
            time.sleep(delay)
            return ok_result(f"slept {delay}")

        return body

    delays = {"list_tables": 0.3, "describe_table": 0.1, "get_metric": 0.2}
    tools = {name: SyncTool(name, sleeper(d)) for name, d in delays.items()}
    calls = [call(name, f"id_{name}") for name in delays]
    ctx = make_tool_ctx()
    started = time.perf_counter()
    results = _run(ctx, tools, calls)
    elapsed = time.perf_counter() - started
    assert [r.tool_call_id for r in results] == [f"id_{n}" for n in delays]
    assert [r.name for r in results] == list(delays)
    assert [r.content for r in results] == [f"slept {d}" for d in delays.values()]
    assert elapsed < sum(delays.values()) - 0.1
    assert all(r.duration_ms >= 90 for r in results)
    events = _events(ctx)
    assert [e["tool"] for e in events] == list(delays)
    assert all(e["ok"] is True and e["step"] == 0 for e in events)
    assert all(e["payload"] == {"args": {}} for e in events)


def test_ut05_70_async_tool_awaited(cfg: None) -> None:
    """UT05-70 an async tool is awaited on the loop and its result filled."""
    del cfg

    async def body(_ctx: ToolContext, **kw: JsonValue) -> ToolResult:
        await asyncio.sleep(0)
        return ok_result(f"n={kw['n']}")

    tool = AsyncFnTool("get_scores", body, schema=N_SCHEMA)
    results = _run(make_tool_ctx(), {"get_scores": tool}, [call("get_scores", "a1", n=7)])
    assert results[0].ok
    assert results[0].content == "n=7"
    assert results[0].tool_call_id == "a1"
    assert tool.calls == [{"n": 7}]


# --- UT05-71 ---------------------------------------------------------------------------------


def test_ut05_71_identical_call_rejected(cfg: None) -> None:
    """UT05-71 same call twice: the second gets "identical call already made at step k"."""
    del cfg
    tool = SyncTool("get_metric", schema=N_SCHEMA)
    state = make_state()
    state.step = 2
    ctx = make_tool_ctx()
    results = _run(ctx, {"get_metric": tool}, [call("get_metric", "a", n=1)] * 2, state)
    assert results[0].ok
    assert not results[1].ok
    assert results[1].error is not None
    assert results[1].error.type == "ToolInputError"
    assert results[1].error.message == "identical call already made at step 2"
    assert tool.calls == [{"n": 1}]
    state.step = 5  # a later message repeating it still names the first step
    later = _run(ctx, {"get_metric": tool}, [call("get_metric", "b", n=1)], state)
    assert later[0].error is not None
    assert later[0].error.message == "identical call already made at step 2"
    assert len(tool.calls) == 1


def test_ut05_71_failed_validation_still_remembered(cfg: None) -> None:
    """UT05-71 a call that failed schema validation counts as made (step 7)."""
    del cfg
    tool = SyncTool("get_metric", schema=N_SCHEMA)
    state = make_state()
    first = _run(make_tool_ctx(), {"get_metric": tool}, [call("get_metric", n="x")], state)
    again = _run(make_tool_ctx(), {"get_metric": tool}, [call("get_metric", n="x")], state)
    assert first[0].error is not None
    assert first[0].error.message == "invalid arguments"
    assert again[0].error is not None
    assert again[0].error.message == "identical call already made at step 0"
    assert tool.calls == []


# --- UT05-72 ---------------------------------------------------------------------------------


def test_ut05_72_schema_violation_has_path_hint(cfg: None) -> None:
    """UT05-72 arguments violating the schema: ToolInputError with the JSON path in the hint."""
    del cfg
    item = strict_schema({"col": {"type": "string"}})
    schema = strict_schema({"filters": {"type": "array", "items": item}})
    tool = SyncTool("get_scores", schema=schema)
    bad = call("get_scores", filters=[{"col": "a"}, {"col": 5}])
    results = _run(make_tool_ctx(), {"get_scores": tool}, [bad])
    err = results[0].error
    assert err is not None
    assert err.type == "ToolInputError"
    assert err.message == "invalid arguments"
    assert err.hint is not None
    assert err.hint.startswith("$.filters[1].col: ")
    assert "is not of type 'string'" in err.hint
    assert "HINT: $.filters[1].col" in results[0].content
    assert tool.calls == []


def test_ut05_72_hint_bounded_and_value_not_echoed(cfg: None) -> None:
    """UT05-72 the hint is <= 300 chars and never echoes the offending argument value."""
    del cfg
    probe = "PROBE-VALUE-" + "Z" * 5_000
    tool = SyncTool("get_metric", schema=N_SCHEMA)
    results = _run(make_tool_ctx(), {"get_metric": tool}, [call("get_metric", n=probe)])
    err = results[0].error
    assert err is not None
    assert err.hint is not None
    assert len(err.hint) <= 300
    assert "PROBE-VALUE" not in err.hint
    assert "PROBE-VALUE" not in results[0].content
    assert err.hint.startswith("$.n: ")


def test_ut05_72_extra_property_names_not_echoed(cfg: None) -> None:
    """UT05-72 an unexpected property's model-supplied name is not echoed in the hint."""
    del cfg
    tool = SyncTool("get_metric", schema=N_SCHEMA)
    name = "PROBE-NAME-" + "Q" * 40
    bad = ToolCall(id="c1", name="get_metric", arguments={"n": 1, name: "x"})
    results = _run(make_tool_ctx(), {"get_metric": tool}, [bad])
    err = results[0].error
    assert err is not None
    assert err.hint == "$: additionalProperties failed; property names not shown"
    assert "PROBE-NAME" not in results[0].content
    assert tool.calls == []


# --- UT05-73 ---------------------------------------------------------------------------------


def test_ut05_73_unknown_tool_lists_allowed(cfg: None) -> None:
    """UT05-73 a name not in the resolved set: error listing the sorted allowed names."""
    del cfg
    tools = {"run_sql": SyncTool("run_sql"), "list_tables": SyncTool("list_tables")}
    results = _run(make_tool_ctx(), tools, [call("drop_everything")])
    err = results[0].error
    assert err is not None
    assert err.type == "ToolInputError"
    assert err.message == "tool drop_everything not allowed; allowed: list_tables, run_sql"
    assert results[0].name == "drop_everything"


# --- UT05-74 ---------------------------------------------------------------------------------


def test_ut05_74_recoverable_errors_become_results(cfg: None) -> None:
    """UT05-74 QueryError and PolicyViolation from a tool: ERROR/HINT error results."""
    del cfg

    def query_error(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        msg = "column x not found"
        raise QueryError(msg, hint="check names with describe_table")

    def policy(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        msg = "record not visible to this role"
        raise PolicyViolation(msg, hint="use aggregates")

    tools = {
        "get_record": SyncTool("get_record", query_error),
        "get_cluster": SyncTool("get_cluster", policy),
    }
    ctx = make_tool_ctx()
    results = _run(ctx, tools, [call("get_record", "r1"), call("get_cluster", "r2")])
    assert results[0].content == (
        "ERROR QueryError: column x not found\nHINT: check names with describe_table"
    )
    assert results[1].content == (
        "ERROR PolicyViolation: record not visible to this role\nHINT: use aggregates"
    )
    assert [r.tool_call_id for r in results] == ["r1", "r2"]
    events = _events(ctx)
    assert [e["error_type"] for e in events] == ["QueryError", "PolicyViolation"]


# --- UT05-75 ---------------------------------------------------------------------------------


def test_ut05_75_store_busy_retried_then_ok(cfg: None) -> None:
    """UT05-75 StoreBusy twice then ok: retried under `tool_store`, ok result."""
    del cfg
    attempts: list[int] = []

    def flaky(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        attempts.append(1)
        if len(attempts) < 3:
            msg = "database is locked"
            raise StoreBusy(msg)
        return ok_result("done")

    results = _run(
        make_tool_ctx(),
        {"list_findings": SyncTool("list_findings", flaky)},
        [call("list_findings")],
    )
    assert results[0].ok
    assert results[0].content == "done"
    assert len(attempts) == 3


def test_ut05_75_store_busy_always_is_error_result(cfg: None) -> None:
    """UT05-75 StoreBusy every time: an error result after the policy's 3 attempts."""
    del cfg
    attempts: list[int] = []

    async def busy(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        attempts.append(1)
        msg = "database is locked"
        raise StoreBusy(msg)

    tool = AsyncFnTool("list_findings", busy)
    results = _run(make_tool_ctx(), {"list_findings": tool}, [call("list_findings")])
    assert results[0].error is not None
    assert results[0].error.type == "StoreBusy"
    assert len(attempts) == 3


# --- UT05-76 ---------------------------------------------------------------------------------


def test_ut05_76_fatal_propagates_and_cancels_siblings(cfg: None) -> None:
    """UT05-76 a tool raising ConfigError: it propagates (not wrapped); siblings cancelled."""
    del cfg
    cancelled = threading.Event()

    async def slow(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            cancelled.set()
            raise
        return ok_result()

    async def fatal(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        await asyncio.sleep(0.01)
        msg = "tool misconfigured"
        raise ConfigError(msg)

    tools = {
        "get_scores": AsyncFnTool("get_scores", slow),
        "get_metric": AsyncFnTool("get_metric", fatal),
    }
    started = time.perf_counter()
    with pytest.raises(ConfigError, match="tool misconfigured"):
        _run(make_tool_ctx(), tools, [call("get_scores", "a"), call("get_metric", "b")])
    assert cancelled.is_set()
    assert time.perf_counter() - started < 5


def test_ut05_76_foreign_exception_from_sync_tool_is_fatal(cfg: None) -> None:
    """UT05-76 an unclassified exception in a sync tool is classified FatalError and raised."""
    del cfg

    def broken(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        msg = "bug"
        raise RuntimeError(msg)

    with pytest.raises(FatalError, match="unclassified RuntimeError"):
        _run(
            make_tool_ctx(), {"get_cluster": SyncTool("get_cluster", broken)}, [call("get_cluster")]
        )


# --- UT05-77 ---------------------------------------------------------------------------------


def test_ut05_77_run_sql_failure_cap(cfg: None) -> None:
    """UT05-77 run_sql failing 3 times on one normalized SQL: 3rd hint "stop retrying…",
    4th not executed."""
    del cfg

    def failing(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        msg = "Binder Error: column zz"
        raise QueryError(msg, hint="check table and column names")

    tool = SyncTool("run_sql", failing, schema=SQL_SCHEMA)
    tools = {"run_sql": tool}
    state, ctx = make_state(), make_tool_ctx()
    variants = ["SELECT zz FROM t", "SELECT  zz FROM t", "SELECT zz FROM t;", "SELECT zz\nFROM t ;"]
    results = [
        _run(ctx, tools, [call("run_sql", f"s{i}", sql=sql)], state)[0]
        for i, sql in enumerate(variants)
    ]
    hints = [r.error.hint if r.error else None for r in results]
    assert hints[:2] == ["check table and column names"] * 2
    assert hints[2] == STOP_HINT
    assert results[2].content == f"ERROR QueryError: Binder Error: column zz\nHINT: {STOP_HINT}"
    assert results[3].error is not None
    assert results[3].error.type == "QueryError"
    assert results[3].error.message == "query failed 3 times"
    assert results[3].error.hint == STOP_HINT
    assert len(tool.calls) == 3
    assert state.sql_failures("SELECT zz FROM t") == 3


def test_ut05_77_other_sql_and_ok_results_not_counted(cfg: None) -> None:
    """UT05-77 ok results and other error types do not count as SQL failures."""
    del cfg

    def policy(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        msg = "blocked column"
        raise PolicyViolation(msg)

    state = make_state()
    _run(
        make_tool_ctx(),
        {"run_sql": SyncTool("run_sql", schema=SQL_SCHEMA)},
        [call("run_sql", sql="SELECT 1")],
        state,
    )
    _run(
        make_tool_ctx(),
        {"run_sql": SyncTool("run_sql", policy, schema=SQL_SCHEMA)},
        [call("run_sql", sql="SELECT 2")],
        state,
    )
    assert state.sql_failures("SELECT 1") == 0
    assert state.sql_failures("SELECT 2") == 0


# --- UT05-78 ---------------------------------------------------------------------------------


def test_ut05_78_max_parallel_bounds_in_flight(
    tmp_path: Path, reset_process_state: ProcessState
) -> None:
    """UT05-78 max_parallel=2 and 4 slow tools: never more than 2 in flight."""
    del reset_process_state
    use_test_config(tmp_path, ("models.harness.tools.max_parallel=2",))
    lock = threading.Lock()
    live = [0]
    peak = [0]

    def slow(_ctx: ToolContext, **_kw: JsonValue) -> ToolResult:
        with lock:
            live[0] += 1
            peak[0] = max(peak[0], live[0])
        time.sleep(0.05)
        with lock:
            live[0] -= 1
        return ok_result()

    tool = SyncTool("get_record", slow, schema=N_SCHEMA)
    calls = [call("get_record", f"c{i}", n=i) for i in range(4)]
    results = _run(make_tool_ctx(), {"get_record": tool}, calls)
    assert all(r.ok for r in results)
    assert peak[0] == 2
    assert len(tool.calls) == 4


# --- UT05-79 ---------------------------------------------------------------------------------


def test_ut05_79_calls_beyond_16_get_errors(cfg: None) -> None:
    """UT05-79 20 calls: calls 17-20 get error results and are not executed."""
    del cfg
    tool = SyncTool("get_record", schema=N_SCHEMA)
    calls = [call("get_record", f"c{i}", n=i) for i in range(20)]
    ctx = make_tool_ctx()
    results = _run(ctx, {"get_record": tool}, calls)
    assert MAX_TOOL_CALLS_PER_MESSAGE == 16
    assert len(results) == 20
    assert all(r.ok for r in results[:16])
    for r in results[16:]:
        assert r.error is not None
        assert r.error.message == "too many tool calls in one message (max 16)"
    assert [r.tool_call_id for r in results] == [f"c{i}" for i in range(20)]
    assert sorted(kw["n"] for kw in tool.calls) == list(range(16))  # type: ignore[type-var]
    assert len(_events(ctx)) == 20


def test_ut05_79_empty_calls_return_empty(cfg: None) -> None:
    """UT05-79 no calls: no results (the loop never dispatches an empty message)."""
    del cfg
    assert _run(make_tool_ctx(), {}, []) == []
