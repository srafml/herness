"""Tests for herness.harness.tools ToolRegistry and tool_registry (impl 05 U05-33).

UT05-68, UT05-69 and UT05-131, plus the `reset_harness_state` fixture of T05-16.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from pydantic import JsonValue
from tests.support.dispatch_standin import FakeWarehouse, SyncTool, make_tool_ctx, strict_schema
from tests.support.harness_state import reset_tool_registry

from herness.core.errors import ConfigError
from herness.core.types import ToolSpec
from herness.harness import tools
from herness.harness.tools import TOOL_OWNERS, ToolRegistry, tool_registry

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class Role:
    """Minimal stand-in for `RoleSpec` (T05-19 not built yet)."""

    name: str
    allowed_tools: frozenset[str]


ANALYST = Role("analyst", frozenset({"run_sql", "list_tables", "post_finding", "list_findings"}))


def _optional_schema() -> dict[str, JsonValue]:
    """One optional property left out of `required` (not strict-compatible, R-26)."""
    return {
        "type": "object",
        "properties": {"q": {"type": "string"}, "k": {"type": ["integer", "null"]}},
        "required": ["q"],
        "additionalProperties": False,
    }


# --- UT05-68 ---------------------------------------------------------------------------------


def test_ut05_68_register_rules() -> None:
    """UT05-68 unknown name, wrong owner, same object twice, different object same name."""
    reg = ToolRegistry()
    with pytest.raises(ConfigError, match="shell"):
        reg.register(SyncTool("shell"), owner="05")
    with pytest.raises(ConfigError, match="owned by 05"):
        reg.register(SyncTool("run_sql"), owner="06")
    tool = SyncTool("run_sql")
    reg.register(tool, owner="05")
    reg.register(tool, owner="05")  # idempotent for the same object
    with pytest.raises(ConfigError, match="already registered"):
        reg.register(SyncTool("run_sql"), owner="05")
    assert reg.names() == ["run_sql"]


def test_ut05_68_names_sorted_and_invalid_schema_refused() -> None:
    """UT05-68 names() is sorted; a schema failing check_schema is a ConfigError."""
    reg = ToolRegistry()
    for name in ("list_tables", "describe_table", "run_sql"):
        reg.register(SyncTool(name), owner="05")
    assert reg.names() == ["describe_table", "list_tables", "run_sql"]
    bad = SyncTool("get_metric", schema={"type": "no-such-type"})
    with pytest.raises(ConfigError, match="get_metric"):
        reg.register(bad, owner="05")
    assert "get_metric" not in reg.names()


def test_ut05_68_tool_owners_is_read_only() -> None:
    """UT05-68 TOOL_OWNERS cannot be extended at run time (TH05-07)."""
    with pytest.raises(TypeError):
        TOOL_OWNERS["shell"] = "05"  # type: ignore[index]
    assert "shell" not in TOOL_OWNERS


# --- UT05-69 ---------------------------------------------------------------------------------


def test_ut05_69_resolve_rejects_names_outside_allow_list() -> None:
    """UT05-69 a name outside role.allowed_tools is a ConfigError naming the role."""
    reg = ToolRegistry()
    reg.register(SyncTool("run_sql"), owner="05")
    reg.register(SyncTool("escalate"), owner="06")
    with pytest.raises(ConfigError, match=r"escalate.*not allowed for role analyst"):
        reg.resolve(ANALYST, ["run_sql", "escalate"], {})


def test_ut05_69_resolve_order_and_task_tool_override() -> None:
    """UT05-69 task tool used over the registered one; result in sorted(names) order."""
    reg = ToolRegistry()
    registered = SyncTool("post_finding")
    run_sql, list_tables = SyncTool("run_sql"), SyncTool("list_tables")
    reg.register(run_sql, owner="05")
    reg.register(list_tables, owner="05")
    reg.register(registered, owner="06")
    task_tool = SyncTool("post_finding")
    got = reg.resolve(
        ANALYST, ["run_sql", "post_finding", "list_tables"], {"post_finding": task_tool}
    )
    assert got == [list_tables, task_tool, run_sql]
    assert reg.resolve(ANALYST, ["post_finding"], {}) == [registered]


def test_ut05_69_resolve_unregistered_and_bad_task_tools() -> None:
    """UT05-69 unregistered name; task tool with a mismatched key or a non-strict schema."""
    reg = ToolRegistry()
    with pytest.raises(ConfigError, match="tool list_tables not registered"):
        reg.resolve(ANALYST, ["list_tables"], {})
    with pytest.raises(ConfigError, match="list_findings"):
        reg.resolve(ANALYST, ["list_findings"], {"list_findings": SyncTool("post_finding")})
    loose = SyncTool("list_findings", schema=_optional_schema())
    with pytest.raises(ConfigError, match="not strict-compatible"):
        reg.resolve(ANALYST, ["list_findings"], {"list_findings": loose})


def test_ut05_69_ctx_build_mismatch() -> None:
    """UT05-69 a ToolContext whose build differs from its warehouse's is a ConfigError."""
    with pytest.raises(ConfigError, match="build_id"):
        make_tool_ctx(warehouse=FakeWarehouse("20260101-000000-ZZZZZZ"))


# --- UT05-131 --------------------------------------------------------------------------------


@pytest.mark.parametrize(("name", "owner"), [("post_finding", "06"), ("recall_memory", "07")])
def test_ut05_131_strict_rule_for_06_and_07_tools(name: str, owner: str) -> None:
    """UT05-131 an optional property outside `required` is refused; the strict form registers."""
    reg = ToolRegistry()
    with pytest.raises(ConfigError, match=f"tool {name} schema is not strict-compatible"):
        reg.register(SyncTool(name, schema=_optional_schema()), owner=owner)  # type: ignore[arg-type]
    fixed = _optional_schema()
    fixed["required"] = ["q", "k"]
    tool = SyncTool(name, schema=fixed)
    reg.register(tool, owner=owner)  # type: ignore[arg-type]
    specs = reg.tool_specs([tool])
    assert specs == [ToolSpec(name=name, description="test tool", input_schema=fixed, strict=True)]


@pytest.mark.parametrize(
    "schema",
    [
        {"type": "object", "properties": {}, "required": []},  # additionalProperties missing
        strict_schema({"f": {"type": "object", "properties": {"a": {"type": "string"}}}}),
        strict_schema({"f": {"type": "array", "items": _optional_schema()}}),
        strict_schema({"f": {"anyOf": [{"type": "null"}, _optional_schema()]}}),
        {**strict_schema(), "$defs": {"x": _optional_schema()}},
    ],
)
def test_ut05_131_strict_rule_at_every_object_level(schema: dict[str, JsonValue]) -> None:
    """UT05-131 nested objects (properties, items, anyOf, $defs) must be strict too."""
    with pytest.raises(ConfigError, match="not strict-compatible"):
        ToolRegistry().register(SyncTool("get_metric", schema=schema), owner="05")


def test_ut05_131_nested_strict_schema_registers() -> None:
    """UT05-131 a nested schema strict at every level registers."""
    inner = strict_schema({"a": {"type": ["string", "null"]}})
    schema = strict_schema({"f": {"type": "array", "items": inner}, "g": {"anyOf": [inner]}})
    ToolRegistry().register(SyncTool("get_metric", schema=schema), owner="05")


# --- tool_registry / reset_harness_state -----------------------------------------------------


def test_ut05_68_process_registry_and_reset() -> None:
    """UT05-68 tool_registry() is one process instance; the T05-16 fixture reset drops it."""
    first = tool_registry()
    assert tool_registry() is first
    first.register(SyncTool("run_sql"), owner="05")
    reset_tool_registry()
    fresh = tool_registry()
    assert fresh is not first
    assert fresh.names() == []
    assert tools.tool_registry() is fresh
