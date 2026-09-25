"""Tests for herness.core.types.harness.tooling (U05-05, U05-06, U05-09)."""

import inspect
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError
from tests.support.harness_fakes import FakeLedger, FakeOps, FakeVectors, RecordingTracer

from herness.core.errors import BudgetExceeded, ConfigError, QueryError, RecoverableError
from herness.core.types import (
    AsyncTool,
    BudgetLedger,
    Budgets,
    OpsHandle,
    SqlLimits,
    Tool,
    ToolContext,
    ToolErrorInfo,
    ToolResult,
    TraceEmitter,
    VectorHandle,
    VectorHit,
    WarehouseHandle,
)

pytestmark = pytest.mark.unit

BUILD = "20260101-000000-ABCDEF"


def _budgets(**overrides: Any) -> Budgets:
    fields: dict[str, Any] = {
        "max_steps": 10,
        "max_tokens": 1_000,
        "max_cost_usd": Decimal("0.50"),
        "wall_clock_s": 60,
    }
    fields.update(overrides)
    return Budgets(**fields)


def test_ut05_04_from_error_with_and_without_hint() -> None:
    """UT05-04 from_error builds `ERROR T: m` content, with `\\nHINT: h` when a hint exists."""
    plain = ToolResult.from_error(RecoverableError("bad column", details={"secret": "x"}))
    assert plain.ok is False
    assert plain.content == "ERROR RecoverableError: bad column"
    assert plain.error == ToolErrorInfo(type="RecoverableError", message="bad column", hint=None)
    assert "secret" not in plain.content

    own_hint = ToolResult.from_error(QueryError("no such table", hint="use list_tables"))
    assert own_hint.content == "ERROR QueryError: no such table\nHINT: use list_tables"
    assert own_hint.error is not None
    assert own_hint.error.hint == "use list_tables"

    override = ToolResult.from_error(QueryError("m", hint="exc hint"), hint="arg hint")
    assert override.content == "ERROR QueryError: m\nHINT: arg hint"
    assert (override.tool_call_id, override.name, override.duration_ms) == ("", "", 0)


def test_ut05_04_from_error_bounds_message_and_hint() -> None:
    """UT05-04 from_error cuts the message to 2,000 and the hint to 500 chars."""

    class _LongError(RecoverableError):
        def __str__(self) -> str:
            return "m" * 3_000

    result = ToolResult.from_error(_LongError("m"), hint="h" * 900)
    assert result.error is not None
    assert result.error.message == "m" * 2_000
    assert result.error.type == "_LongError"
    assert result.error.hint == "h" * 500


def test_ut05_04_content_truncated_to_12000() -> None:
    """UT05-04 20,000-char content is cut to 12,000 chars ending in … with truncated set."""
    result = ToolResult(ok=True, content="x" * 20_000)
    assert len(result.content) == 12_000
    assert result.content.endswith("…")
    assert result.truncated is True
    exact = ToolResult(ok=True, content="y" * 12_000)
    assert exact.content == "y" * 12_000
    assert exact.truncated is False
    assert ToolResult.model_validate_json(result.model_dump_json()) == result


def test_ut05_04_ok_error_pairing_and_ids() -> None:
    """UT05-04 ok is False exactly when error is set; id lists follow their patterns."""
    info = ToolErrorInfo(type="QueryError", message="m", hint=None)
    with pytest.raises(ValidationError):
        ToolResult(ok=True, content="c", error=info)
    with pytest.raises(ValidationError):
        ToolResult(ok=False, content="c")
    good = ToolResult(
        ok=True,
        content="c",
        query_ids=["q_0123456789abcdef"],
        finding_ids=["fnd_01HZX3K8Q9V2M4N6P7R8S9T0VW"],
        row_count=3,
    )
    assert good.row_count == 3
    for bad in ({"query_ids": ["q_XYZ"]}, {"finding_ids": ["fnd_short"]}, {"row_count": -1}):
        with pytest.raises(ValidationError):
            ToolResult(ok=True, content="c", **bad)
    with pytest.raises(ValidationError):
        ToolErrorInfo(type="E", message="m" * 2_001, hint=None)
    with pytest.raises(ValidationError):
        good.content = "changed"  # type: ignore[misc]
    filled = good.model_copy(update={"tool_call_id": "c1", "name": "run_sql"})
    assert (filled.tool_call_id, filled.name) == ("c1", "run_sql")


def test_ut05_05_budgets_build_with_and_without_deadline() -> None:
    """UT05-05 Budgets build with and without deadline; aware deadlines are kept in UTC."""
    assert _budgets().deadline is None
    deadline = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
    assert _budgets(deadline=deadline).deadline == deadline
    plus_two = timezone(timedelta(hours=2))
    shifted = _budgets(deadline=datetime(2026, 9, 25, 14, 0, tzinfo=plus_two))
    assert shifted.deadline == deadline
    assert shifted.deadline is not None
    assert shifted.deadline.utcoffset() == timedelta(0)
    assert _budgets(max_cost_usd=Decimal(0)).max_cost_usd == 0


@pytest.mark.parametrize(
    "overrides",
    [
        {"deadline": datetime(2026, 9, 25, 12, 0)},  # noqa: DTZ001 - naive on purpose
        {"max_steps": 0},
        {"max_steps": 201},
        {"max_tokens": 999},
        {"max_cost_usd": Decimal("-0.01")},
        {"wall_clock_s": 0},
        {"wall_clock_s": 86_401},
        {"max_steps": True},
    ],
)
def test_ut05_05_budgets_reject_naive_and_out_of_range(overrides: dict[str, Any]) -> None:
    """UT05-05 a naive deadline and out-of-range values raise ValidationError."""
    with pytest.raises(ValidationError):
        _budgets(**overrides)


def test_ut05_05_no_from_task_budget_and_sql_limits() -> None:
    """UT05-05 Budgets has no from_task_budget (R-23); SqlLimits defaults match design §4.4."""
    assert not hasattr(Budgets, "from_task_budget")
    limits = SqlLimits(timeout_s=30.0)
    assert (limits.return_rows, limits.scan_rows, limits.max_attempts_per_query) == (
        200,
        1_000_000,
        3,
    )
    for timeout in (0, True, float("inf"), float("nan")):
        with pytest.raises(ValidationError):
            SqlLimits(timeout_s=timeout)
    with pytest.raises(ValidationError):
        _budgets().max_steps = 5  # type: ignore[misc]


class _Warehouse:
    def __init__(self, build_id: str) -> None:
        self.build_id = build_id
        self.path = Path("wh.duckdb")

    def cursor(self) -> object:
        return object()

    def schema(self) -> dict[str, dict[str, dict[str, str]]]:
        return {"core": {"ticket": {"id": "varchar"}}}

    def table_comment(self, qualified: str) -> str:
        return qualified


class _SyncTool:
    name = "echo"
    description = "echo"
    input_schema: dict[str, Any] = {"type": "object"}  # noqa: RUF012 - protocol attribute

    def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:
        return ToolResult(ok=True, content=ctx.task_id)


class _AsyncTool(_SyncTool):
    async def __call__(self, ctx: ToolContext, **kwargs: Any) -> ToolResult:  # type: ignore[override]
        return ToolResult(ok=True, content=ctx.run_id)


def _context(warehouse_build: str = BUILD, **overrides: Any) -> ToolContext:
    fields: dict[str, Any] = {
        "run_id": "run_1",
        "task_id": "task_1",
        "build_id": BUILD,
        "role": "analyst",
        "specialty": "backlog",
        "depth": "standard",
        "profile": "local",
        "tool_names": ["echo"],
        "warehouse": _Warehouse(warehouse_build),
        "ops": FakeOps(),
        "vectors": FakeVectors(),
        "budgets": _budgets(),
        "ledger": FakeLedger(),
        "sql_limits": SqlLimits(timeout_s=10.0),
        "tracer": RecordingTracer("run_1", "task_1"),
    }
    fields.update(overrides)
    return ToolContext(**fields)


def test_ut05_69_tool_context_build_mismatch_and_handles() -> None:
    """UT05-69 ctx build mismatch raises ConfigError; handles are excluded from dumps."""
    with pytest.raises(ConfigError, match="tool context build_id"):
        _context(warehouse_build="20260101-000000-ZZZZZZ")
    ctx = _context(task_tools={"echo": _SyncTool(), "later": _AsyncTool()})
    dumped = ctx.model_dump()
    for handle in ("task_tools", "warehouse", "ops", "vectors", "ledger", "tracer"):
        assert handle not in dumped
    assert dumped["text_access"] == "redacted_only"
    assert dumped["egress_purpose"] is None
    with pytest.raises(ValidationError):
        _context(ops=object())
    with pytest.raises(ValidationError):
        _context(text_access="full")
    assert isinstance(ctx.task_tools["echo"], Tool)
    assert isinstance(ctx.task_tools["later"], AsyncTool)
    assert not inspect.iscoroutinefunction(type(ctx.task_tools["echo"]).__call__)
    assert inspect.iscoroutinefunction(type(ctx.task_tools["later"]).__call__)
    assert ctx.task_tools["echo"](ctx).content == "task_1"
    assert isinstance(ctx.warehouse, WarehouseHandle)
    assert isinstance(ctx.ops, OpsHandle)
    assert isinstance(ctx.vectors, VectorHandle)
    assert isinstance(ctx.ledger, BudgetLedger)
    assert isinstance(ctx.tracer, TraceEmitter)


def test_ut05_69_fakes_behave_like_handles() -> None:
    """UT05-69 the shared fakes record charges, spans and vector searches."""
    ledger = FakeLedger(max_cost_usd=Decimal("1.00"))
    ledger.charge(10, 5, Decimal("0.40"))
    assert ledger.snapshot() == {"tokens_in": 10, "tokens_out": 5, "cost_usd": "0.40"}
    with pytest.raises(BudgetExceeded, match="budget"):
        ledger.charge(1, 1, Decimal("0.70"))
    tracer = RecordingTracer("run_1", None)
    assert tracer.emit("tool_call", name="echo") == "sp_000001"
    assert tracer.events == [("tool_call", "sp_000001", {"name": "echo"})]
    assert (tracer.run_id, tracer.task_id) == ("run_1", None)
    hit = VectorHit(
        record_id="jira:issue:1", entity="issue", service_id=None, opened_at=None, similarity=0.9
    )
    naive = datetime(2026, 9, 25)  # noqa: DTZ001 - naive on purpose
    for bad in ({"opened_at": naive}, {"similarity": float("nan")}):
        with pytest.raises(ValidationError):
            VectorHit.model_validate({**hit.model_dump(), **bad})
    vectors = FakeVectors([hit])
    assert vectors.search_tickets([0.1, 0.2], 5, entity="issue", service_id=None) == [hit]
    assert vectors.calls == [([0.1, 0.2], 5, "issue", None)]
    ops = FakeOps(finding_statuses={"fnd_1": "verified"})
    assert ops.finding_statuses(["fnd_1", "fnd_2"]) == {"fnd_1": "verified"}
    assert ops.get_evidence("q_0123456789abcdef") is None
    used_at = datetime(2026, 9, 25, tzinfo=UTC)
    assert ops.record_evidence_use("q_0123456789abcdef", "run_1", None, used_at) is True
    assert ops.record_evidence_use("q_0123456789abcdef", "run_1", None, used_at) is False
