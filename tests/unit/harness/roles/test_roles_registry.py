"""Tests for herness.harness.roles get_role, ROLE_NAMES and the part-1 roles (impl 05 U05-51).

UT05-92 for planner, judge and the seven analysts, plus every error combination (T05-19);
T05-20 extends it with skeptic, writer and chat.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from tests.support.dispatch_standin import SyncTool, strict_schema

from herness.core.errors import ConfigError
from herness.core.types import PlannedTask
from herness.harness.roles import base
from herness.harness.roles.analyst import ANALYST_SPECIALTIES, AnalystOutput, analyst_role
from herness.harness.roles.base import ROLE_NAMES, RoleSpec, get_role
from herness.harness.roles.judge import JUDGE, JudgeOutput
from herness.harness.roles.planner import PLANNER, PlannerOutput
from herness.harness.tools import TOOL_OWNERS, ToolRegistry

pytestmark = pytest.mark.unit

_PLANNER_TOOLS = {"list_tables", "describe_table", "get_scores", "get_metric", "recall_memory"}
_ANALYST_TOOLS = {
    "list_tables",
    "describe_table",
    "run_sql",
    "get_metric",
    "get_scores",
    "get_cluster",
    "get_record",
    "semantic_search",
    "recall_memory",
    "propose_memory",
    "post_finding",
    "list_findings",
    "request_subtask",
}
_PART_ONE = ("planner", "judge", *(f"analyst_{s}" for s in ANALYST_SPECIALTIES))


def test_ut05_92_role_names() -> None:
    """UT05-92 ROLE_NAMES is the spec 06 role list without verifier_claim (R-37)."""
    assert ROLE_NAMES == (
        "planner",
        "judge",
        "analyst_ops",
        "analyst_change",
        "analyst_delivery",
        "analyst_org",
        "analyst_crosscheck",
        "analyst_retrospective",
        "analyst_general",
        "skeptic",
        "writer",
        "chat",
    )
    assert ANALYST_SPECIALTIES == (
        "ops",
        "change",
        "delivery",
        "org",
        "crosscheck",
        "retrospective",
        "general",
    )


def test_ut05_92_planner_and_judge_table() -> None:
    """UT05-92 planner and judge table values (Planner temperature 0.2, R-28)."""
    planner = get_role("planner")
    assert planner is PLANNER
    assert (planner.name, planner.specialty, planner.model_role) == ("planner", None, "planner")
    assert planner.allowed_tools == frozenset(_PLANNER_TOOLS)
    assert planner.output_model is PlannerOutput
    assert (planner.temperature, planner.effort, planner.thinking) == (0.2, "high", "auto")
    assert planner.prompt_files == ("_common.md", "planner.md")
    judge = get_role("judge")
    assert judge is JUDGE
    assert judge.allowed_tools == frozenset()
    assert judge.output_model is JudgeOutput
    assert (judge.temperature, judge.effort, judge.thinking) == (0.0, "medium", "off")
    assert (judge.model_role, judge.prompt_files) == ("judge", ("_common.md", "judge.md"))


@pytest.mark.parametrize("specialty", ANALYST_SPECIALTIES)
def test_ut05_92_analyst_table(specialty: str) -> None:
    """UT05-92 each analyst specialty: tools, output, sampling, model role and prompts."""
    role = get_role(f"analyst_{specialty}")
    assert role is analyst_role(specialty)
    assert (role.name, role.specialty) == (f"analyst_{specialty}", specialty)
    assert role.allowed_tools == frozenset(_ANALYST_TOOLS)
    assert role.output_model is AnalystOutput
    assert (role.temperature, role.effort, role.thinking) == (0.2, "medium", "auto")
    assert role.model_role == "analyst"
    assert role.prompt_files == ("_common.md", f"analyst_{specialty}.md")


def test_ut05_92_default_model_role_override() -> None:
    """UT05-92 passing the role's own default model_role returns an equal role."""
    assert get_role("planner", model_role="planner") == PLANNER
    assert get_role("analyst_ops", model_role="analyst") == analyst_role("ops")


@pytest.mark.parametrize(
    ("name", "kwargs", "message"),
    [
        ("verifier_claim", {}, "unknown role"),
        ("analyst", {}, "unknown role"),
        ("nope", {}, "unknown role"),
        ("planner", {"variant": "retrospective"}, "variant"),
        ("analyst_retrospective", {"variant": "retrospective"}, "variant"),
        ("planner", {"variant": "other"}, "variant"),
        ("planner", {"model_role": "judge"}, "model role"),
        ("judge", {"model_role": "skeptic_final"}, "model role"),
        ("analyst_ops", {"model_role": "chat_off_hours"}, "model role"),
        ("skeptic", {"model_role": "chat"}, "model role"),
        ("chat", {"model_role": "skeptic_final"}, "model role"),
        ("writer", {"model_role": "planner"}, "model role"),
        ("skeptic", {"variant": "retrospective"}, "variant"),
    ],
)
def test_ut05_92_bad_combinations(name: str, kwargs: dict[str, str], message: str) -> None:
    """UT05-92 unknown names (verifier_claim, R-37), bad variants, disallowed model roles."""
    with pytest.raises(ConfigError, match=message):
        get_role(name, **kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("name", "kwargs"),
    [
        ("skeptic", {}),
        ("skeptic", {"model_role": "skeptic_final"}),
        ("chat", {"model_role": "chat_off_hours"}),
        ("writer", {"variant": "retrospective"}),
        ("writer", {}),
    ],
)
def test_ut05_92_part_two_roles_not_yet_available(name: str, kwargs: dict[str, str]) -> None:
    """UT05-92 skeptic/writer/chat pass the preconditions, then are not available (T05-20)."""
    with pytest.raises(ConfigError, match=f"role {name} not available"):
        get_role(name, **kwargs)  # type: ignore[arg-type]


def test_ut05_92_model_role_replaced(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-92 an allowed non-default model_role replaces the role's model_role."""
    monkeypatch.setitem(base._LOCATIONS, "skeptic", ("planner", "PLANNER"))
    role = get_role("skeptic", model_role="skeptic_final")
    assert role.model_role == "skeptic_final"
    assert role.allowed_tools == PLANNER.allowed_tools


def test_ut05_92_missing_attribute(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT05-92 a role module without the constant → ConfigError, not AttributeError."""
    monkeypatch.setitem(base._LOCATIONS, "chat", ("planner", "CHAT"))
    with pytest.raises(ConfigError, match="role chat not available"):
        get_role("chat")


def test_ut05_92_unknown_specialty() -> None:
    """UT05-92 analyst_role of an unknown specialty → ConfigError."""
    with pytest.raises(ConfigError, match="unknown analyst specialty"):
        analyst_role("finance")


@pytest.mark.parametrize("name", _PART_ONE)
def test_ut05_92_tools_resolve_strict(name: str) -> None:
    """UT05-92 every allow-list is within TOOL_OWNERS and resolves with strict tools (TH05-07)."""
    role = get_role(name)
    assert isinstance(role, RoleSpec)
    assert isinstance(role.allowed_tools, frozenset)
    assert role.allowed_tools <= TOOL_OWNERS.keys()
    registry = ToolRegistry()
    task_tools = {}
    for tool_name in sorted(role.allowed_tools):
        tool = SyncTool(tool_name, schema=strict_schema())
        owner = TOOL_OWNERS[tool_name]
        if owner == "06":
            task_tools[tool_name] = tool
        else:
            registry.register(tool, owner=owner)  # type: ignore[arg-type]
    resolved = registry.resolve(role, sorted(role.allowed_tools), task_tools)
    assert [t.name for t in resolved] == sorted(role.allowed_tools)


def _planned() -> dict[str, object]:
    return {
        "dedup_key": None,
        "specialty": "ops",
        "objective": "Check MTTR by service",
        "entity_type": "service",
        "entity_ids": ["svc-1"],
        "notes": None,
    }


def test_ut05_92_planner_output_model() -> None:
    """UT05-92 PlannerOutput holds 1-200 PlannedTask items and forbids extra keys."""
    out = PlannerOutput.model_validate({"tasks": [_planned()], "rationale": "r", "unknowns": []})
    assert isinstance(out.tasks[0], PlannedTask)
    for bad in (
        {"tasks": [], "rationale": "r", "unknowns": []},
        {"tasks": [_planned()] * 201, "rationale": "r", "unknowns": []},
        {"tasks": [_planned()], "rationale": "r" * 4_001, "unknowns": []},
        {"tasks": [_planned()], "rationale": "r", "unknowns": ["u"] * 51},
        {"tasks": [_planned()], "rationale": "r", "unknowns": [], "extra": 1},
    ):
        with pytest.raises(ValidationError):
            PlannerOutput.model_validate(bad)


def test_ut05_92_judge_output_model() -> None:
    """UT05-92 JudgeOutput: scores 0-5 (1-10), choice < len(scores), extra keys forbidden."""
    out = JudgeOutput.model_validate({"choice": 1, "scores": [1, 4.5], "reasons": ["a"]})
    assert out.scores == [1.0, 4.5]
    for bad in (
        {"choice": 2, "scores": [1, 2], "reasons": []},
        {"choice": -1, "scores": [1], "reasons": []},
        {"choice": 0, "scores": [], "reasons": []},
        {"choice": 0, "scores": [5.5], "reasons": []},
        {"choice": 0, "scores": [1.0] * 11, "reasons": []},
        {"choice": 0, "scores": [1], "reasons": ["r"] * 11},
        {"choice": 0, "scores": [1], "reasons": [], "extra": 1},
    ):
        with pytest.raises(ValidationError):
            JudgeOutput.model_validate(bad)


def test_ut05_92_analyst_output_model() -> None:
    """UT05-92 AnalystOutput field limits and extra keys forbidden."""
    ok = {"summary": "s", "unknowns": [], "suggested_followups": []}
    assert AnalystOutput.model_validate(ok).summary == "s"
    for bad in (
        {**ok, "summary": "s" * 3_001},
        {**ok, "unknowns": ["u"] * 51},
        {**ok, "suggested_followups": ["f"] * 21},
        {**ok, "finding_ids": []},
    ):
        with pytest.raises(ValidationError):
            AnalystOutput.model_validate(bad)
