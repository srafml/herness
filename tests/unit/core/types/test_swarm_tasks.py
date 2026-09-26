"""Tests for herness.core.types.swarm task, finding and challenge types (T06-01)."""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, get_args

import pytest
from pydantic import ValidationError

import herness.core.types as shared
from herness.core.errors import SchemaViolation
from herness.core.types import swarm
from herness.core.types.harness import Budgets, NumberRef, VerificationResult
from herness.core.types.swarm import (
    SKEPTIC_CHECKS,
    Banner,
    Challenge,
    CheckResult,
    CrossCheck,
    Depth,
    EntityScope,
    Finding,
    FindingStatus,
    PlannedTask,
    RejectReason,
    Role,
    RunKind,
    ScopeEntityType,
    SectionId,
    SkepticCheck,
    Specialty,
    SwarmTaskState,
    TaskBudget,
    TaskInputs,
    TaskSpec,
    VerificationRecord,
)

pytestmark = pytest.mark.unit

_ULID = "01J9ZQ4Y8M6V3K2N1P0R5T7W9X"
_RUN = f"run_{_ULID}"
_TASK = f"task_{_ULID}"
_FND = f"fnd_{_ULID}"
_FND2 = "fnd_01J9ZQ4Y8M6V3K2N1P0R5T7W9Y"
_Q1 = "q_0123456789abcdef"
_Q2 = "q_fedcba9876543210"
_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _budget(**overrides: Any) -> dict[str, Any]:
    return {"max_steps": 12, "max_tokens": 40_000, "wall_clock_s": 300} | overrides


def _spec(**overrides: Any) -> dict[str, Any]:
    return {
        "task_id": _TASK,
        "run_id": _RUN,
        "role": "analyst",
        "objective": "Explain the incident trend",
        "scope": {"entity_type": "service", "entity_ids": ["svc-a"]},
        "tools": ["run_sql", "post_finding"],
        "budget": _budget(),
        "model_role": "analyst",
        "dedup_key": "0123456789abcdef",
    } | overrides


def _ref(ref_id: str = "n1", query: str = _Q1) -> dict[str, Any]:
    return {
        "id": ref_id,
        "value": 3,
        "unit": "count",
        "query_id": query,
        "column": "n",
        "row_key": None,
    }


def _finding(**overrides: Any) -> dict[str, Any]:
    return {
        "finding_id": _FND,
        "run_id": _RUN,
        "task_id": _TASK,
        "author_role": "analyst",
        "claim": "Incidents rose to [[n1]]",
        "entity_type": "service",
        "entity_id": "svc-a",
        "numbers": [_ref()],
        "query_ids": [_Q1],
        "confidence": 0.7,
        "created_at": _NOW,
    } | overrides


def _checks(result: str = "pass") -> list[dict[str, Any]]:
    return [{"check": name, "result": result, "note": "ok"} for name in SKEPTIC_CHECKS]


def _verification() -> VerificationResult:
    return VerificationResult(
        build_id="b1", passed=True, items=[], n_numbers=0, n_failed=0, verified_at=_NOW,
        duration_ms=0,
    )  # fmt: skip


def test_ut06_01_vocabularies_match_design() -> None:
    """UT06-01 the closed vocabularies equal design 06 §4.1-§4.4 exactly."""
    assert get_args(RunKind.__value__) == ("funding_review", "org_review", "chat")
    assert get_args(Depth.__value__) == ("fast", "standard", "deep")
    assert get_args(Role.__value__) == (
        "planner", "judge", "analyst", "skeptic", "verifier", "writer", "chat",
    )  # fmt: skip
    assert get_args(Specialty.__value__) == (
        "ops", "change", "delivery", "org", "crosscheck", "retrospective", "general",
    )  # fmt: skip
    assert get_args(ScopeEntityType.__value__) == (
        "service", "team", "org", "work_item", "cluster", "candidate", "run",
    )  # fmt: skip
    assert SKEPTIC_CHECKS == (
        "confounding", "seasonality", "mis_mapping", "small_sample", "double_counting",
        "survivorship",
    )  # fmt: skip
    assert get_args(SkepticCheck.__value__) == SKEPTIC_CHECKS
    assert get_args(RejectReason.__value__) == (
        "skeptic_reject", "verifier_fail", "crosscheck_disagree", "withdrawn", "revision_dead",
    )  # fmt: skip
    assert get_args(FindingStatus.__value__) == (
        "proposed", "challenged", "verified", "rejected", "revised", "merged",
    )  # fmt: skip
    assert get_args(Banner.__value__) == (
        "dq_warnings", "unconfirmed_weights", "partial_run", "hybrid_fallback",
        "budget_exhausted",
    )  # fmt: skip
    assert get_args(SectionId.__value__) == (
        "executive_summary", "recommendations", "portfolio", "org_scorecards", "actions",
        "retrospective", "risks_and_caveats", "method",
    )  # fmt: skip


@pytest.mark.parametrize(
    "scope",
    [
        {"entity_type": "service", "entity_ids": [f"s{i}" for i in range(51)]},
        {"entity_type": "service", "entity_ids": ["a", "b", "a"]},
        {"entity_type": "service", "entity_ids": []},
        {"entity_type": "service", "entity_ids": ["x" * 201]},
        {"entity_type": "project", "entity_ids": ["a"]},
        {
            "entity_type": "team",
            "entity_ids": ["t1"],
            "period_start": date(2026, 9, 2),
            "period_end": date(2026, 9, 1),
        },
    ],
    ids=["51_ids", "duplicate_ids", "empty", "long_id", "bad_type", "reversed_period"],
)
def test_ut06_01_entity_scope_rejects(scope: dict[str, Any]) -> None:
    """UT06-01 51 ids, duplicate ids, empty list, long id, bad type, reversed period rejected."""
    with pytest.raises(ValidationError):
        EntityScope.model_validate(scope)


def test_ut06_01_entity_scope_keeps_order_and_same_day() -> None:
    """UT06-01 entity_ids keep input order; equal period bounds are valid."""
    scope = EntityScope(
        entity_type="team", entity_ids=["z", "a"], period_start="2026-09-01",  # type: ignore[arg-type]
        period_end=date(2026, 9, 1),
    )  # fmt: skip
    assert scope.entity_ids == ["z", "a"]
    assert scope.period_start == scope.period_end == date(2026, 9, 1)


@pytest.mark.parametrize(
    "inputs",
    [
        {"finding_ids": ["fnd_bad"]},
        {"query_ids": ["q_XYZ"]},
        {"candidate_ids": ["c"] * 201},
        {"dq_warnings": ["w"] * 101},
        {"notes": "x" * 1501},
        {"unknown": 1},
    ],
    ids=["finding_pattern", "query_pattern", "candidates", "dq", "notes", "extra"],
)
def test_ut06_01_task_inputs_rejects(inputs: dict[str, Any]) -> None:
    """UT06-01 TaskInputs pattern, length and extra-field violations are rejected."""
    with pytest.raises(ValidationError):
        TaskInputs.model_validate(inputs)


@pytest.mark.parametrize(
    "budget",
    [
        _budget(max_steps=0),
        _budget(max_tokens=-1),
        _budget(wall_clock_s=0),
        _budget(max_cost_usd="-0.01"),
        _budget(max_cost_usd="0.001"),
    ],
    ids=["steps", "tokens", "wall_clock", "negative_cost", "three_decimals"],
)
def test_ut06_01_task_budget_rejects(budget: dict[str, Any]) -> None:
    """UT06-01 zero or negative limits and costs with more than two decimals are rejected."""
    with pytest.raises(ValidationError):
        TaskBudget.model_validate(budget)


@pytest.mark.parametrize(
    "overrides",
    [
        {"depth": 3},
        {"role": "skeptic", "round": 1},
        {"role": "skeptic", "round": 0, "inputs": {"finding_ids": [_FND]}},
        {"role": "skeptic", "round": 1, "inputs": {"finding_ids": [_FND, _FND2]}},
        {"revision_of": _FND, "round": 1, "role": "writer"},
        {"revision_of": _FND, "round": 0},
        {"k_samples": 10},
        {"round": 11},
        {"objective": ""},
        {"objective": "x" * 2001},
        {"tools": ["run_sql", "run_sql"]},
        {"tools": ["Run-SQL"]},
        {"priority": float("inf")},
        {"model_role": "Analyst"},
        {"dedup_key": "0123"},
        {"task_id": "task_x"},
        {"run_id": _TASK},
        {"parent_task_id": _RUN},
        {"role": "boss"},
    ],
)
def test_ut06_01_task_spec_rejects(overrides: dict[str, Any]) -> None:
    """UT06-01 depth 3, skeptic without one finding and other field violations are rejected."""
    with pytest.raises(ValidationError):
        TaskSpec.model_validate(_spec(**overrides))


def test_ut06_01_task_spec_valid_round_trip() -> None:
    """UT06-01 valid specs (analyst, skeptic, revision) survive a JSON round trip."""
    specs = [
        TaskSpec.model_validate(_spec()),
        TaskSpec.model_validate(
            _spec(role="skeptic", round=1, inputs={"finding_ids": [_FND]}, k_samples=5)
        ),
        TaskSpec.model_validate(
            _spec(revision_of=_FND, round=2, depth=2, parent_task_id=_TASK, priority=-1.5)
        ),
    ]
    for spec in specs:
        again = TaskSpec.model_validate_json(spec.model_dump_json())
        assert again == spec
    default = specs[0]
    assert default.specialty == "general"
    assert default.inputs == TaskInputs()
    assert default.budget.max_cost_usd == Decimal(0)
    assert (default.depth, default.round, default.k_samples, default.must_cover) == (
        0, 0, 1, False,
    )  # fmt: skip
    with pytest.raises(ValidationError):
        default.depth = 1  # type: ignore[misc]


def test_ut06_02_to_budgets_and_scaled() -> None:
    """UT06-02 to_budgets copies fields and sets deadline; scaled halves; naive now fails."""
    budget = TaskBudget(max_steps=12, max_tokens=40_000, wall_clock_s=300, max_cost_usd="0.05")  # type: ignore[arg-type]
    out = budget.to_budgets(_NOW)
    assert isinstance(out, Budgets)
    assert (out.max_steps, out.max_tokens, out.wall_clock_s) == (12, 40_000, 300)
    assert out.max_cost_usd == Decimal("0.05")
    assert out.deadline == _NOW + timedelta(seconds=300)
    half = budget.scaled(0.5)
    assert (half.max_steps, half.max_tokens, half.wall_clock_s) == (6, 20_000, 150)
    assert half.max_cost_usd == Decimal("0.02")
    assert budget.scaled(1.0) == budget
    tiny = TaskBudget(max_steps=1, max_tokens=1_000, wall_clock_s=1).scaled(0.1)
    assert (tiny.max_steps, tiny.max_tokens, tiny.wall_clock_s) == (1, 1_000, 1)
    assert tiny.to_budgets(_NOW).deadline == _NOW + timedelta(seconds=1)
    small = TaskBudget(max_steps=3, max_tokens=1_500, wall_clock_s=10).scaled(0.25)
    assert (small.max_steps, small.max_tokens, small.wall_clock_s) == (1, 1_000, 2)
    assert small.to_budgets(_NOW).max_tokens == 1_000
    widest = TaskBudget(max_steps=200, max_tokens=1_000, wall_clock_s=86_400)
    assert widest.to_budgets(_NOW).wall_clock_s == 86_400
    with pytest.raises(SchemaViolation, match="naive datetime"):
        budget.to_budgets(_NOW.replace(tzinfo=None))


@pytest.mark.parametrize(
    "budget",
    [_budget(max_tokens=999), _budget(max_steps=201), _budget(wall_clock_s=86_401)],
    ids=["tokens_below_1000", "steps_above_200", "wall_clock_above_day"],
)
def test_ut06_02_task_budget_outside_budgets_bounds_rejected(budget: dict[str, Any]) -> None:
    """UT06-02 TaskBudget rejects values spec 05 Budgets would reject, so to_budgets never fails."""
    with pytest.raises(ValidationError):
        TaskBudget.model_validate(budget)


@pytest.mark.parametrize("factor", [0.0, -0.5, 1.5, float("nan")])
def test_ut06_02_scaled_rejects_factor_out_of_range(factor: float) -> None:
    """UT06-02 scaled requires 0 < factor <= 1."""
    with pytest.raises(ValueError, match="factor"):
        TaskBudget(**_budget()).scaled(factor)


def _planned(**overrides: Any) -> dict[str, Any]:
    return {
        "dedup_key": None,
        "specialty": "ops",
        "objective": "Check deploy failures",
        "entity_type": "team",
        "entity_ids": ["t1"],
        "notes": None,
    } | overrides


def test_ut06_03_planned_task_rejects_budget_key() -> None:
    """UT06-03 planner JSON with a budget key is rejected (extra forbidden)."""
    with pytest.raises(ValidationError, match="budget"):
        PlannedTask.model_validate(_planned(budget=_budget()))
    for extra in ("tools", "priority", "model_role", "task_id"):
        with pytest.raises(ValidationError):
            PlannedTask.model_validate(_planned(**{extra: "x"}))


@pytest.mark.parametrize(
    "overrides",
    [
        {"dedup_key": "XYZ"},
        {"objective": ""},
        {"entity_ids": []},
        {"entity_ids": ["e"] * 51},
        {"notes": "n" * 1501},
        {"specialty": "finance"},
    ],
)
def test_ut06_03_planned_task_field_bounds(overrides: dict[str, Any]) -> None:
    """UT06-03 PlannedTask pattern and length violations are rejected."""
    with pytest.raises(ValidationError):
        PlannedTask.model_validate(_planned(**overrides))


def test_ut06_03_planned_task_valid() -> None:
    """UT06-03 a valid planned task validates, with or without a deterministic dedup key."""
    item = PlannedTask.model_validate(_planned(dedup_key="0123456789abcdef", notes="focus"))
    assert item.dedup_key == "0123456789abcdef"
    assert PlannedTask.model_validate(_planned()).notes is None


def test_ut06_04_finding_missing_number_query_id() -> None:
    """UT06-04 a finding missing a number's query id is rejected."""
    with pytest.raises(ValidationError, match="query_ids"):
        Finding.model_validate(_finding(numbers=[_ref(query=_Q2)]))


@pytest.mark.parametrize(
    "overrides",
    [
        {"status": "merged"},
        {"merged_into": _FND2},
        {"numbers": [_ref("n1"), _ref("n1")]},
        {"numbers": []},
        {"numbers": [_ref(f"n{i}") for i in range(21)]},
        {"query_ids": [_Q1, _Q1]},
        {"query_ids": []},
        {"confidence": 1.5},
        {"claim": ""},
        {"claim": "x" * 1501},
        {"entity_id": ""},
        {"entity_type": "project"},
        {"created_at": datetime(2026, 9, 25, 12, 0)},  # noqa: DTZ001 - naive on purpose
        {"finding_id": _TASK},
        {"supersedes": "fnd_bad"},
    ],
)
def test_ut06_04_finding_rejects(overrides: dict[str, Any]) -> None:
    """UT06-04 merged without merged_into and other invariant violations are rejected."""
    with pytest.raises(ValidationError):
        Finding.model_validate(_finding(**overrides))


def test_ut06_04_finding_valid_round_trip() -> None:
    """UT06-04 valid findings (merged, extra query ids, non-UTC time) round-trip through JSON."""
    plus_two = datetime(2026, 9, 25, 14, 0, tzinfo=timezone(timedelta(hours=2)))
    merged = Finding.model_validate(
        _finding(
            status="merged",
            merged_into=_FND2,
            query_ids=[_Q2, _Q1],
            numbers=[_ref("n1"), _ref("n2", _Q2)],
            created_at=plus_two,
        )
    )
    assert merged.created_at.tzinfo == UTC
    assert Finding.model_validate_json(merged.model_dump_json()) == merged
    plain = Finding.model_validate(_finding())
    assert (plain.status, plain.challenge, plain.verification) == ("proposed", [], None)
    assert isinstance(plain.numbers[0], NumberRef)


def test_ut06_06_challenge_rejects_bad_checks_and_verdicts() -> None:
    """UT06-06 5 checks, concern without query and revise without actions are each rejected."""
    base = {"finding_id": _FND, "verdict": "uphold"}
    with pytest.raises(ValidationError):
        Challenge.model_validate(base | {"checks": _checks()[:5]})
    doubled = [*_checks()[:5], _checks()[0]]
    with pytest.raises(ValidationError):
        Challenge.model_validate(base | {"checks": doubled})
    with pytest.raises(ValidationError):
        CheckResult.model_validate({"check": "confounding", "result": "concern", "note": "n"})
    with pytest.raises(ValidationError):
        CheckResult.model_validate({"check": "seasonality", "result": "fail", "note": "n"})
    with pytest.raises(ValidationError):
        Challenge.model_validate(base | {"checks": _checks(), "verdict": "revise"})
    with pytest.raises(ValidationError):
        Challenge.model_validate(base | {"checks": _checks(), "round": 11})
    with pytest.raises(ValidationError):
        Challenge.model_validate(
            base | {"checks": _checks(), "verdict": "revise", "required_actions": ["a" * 401]}
        )


@pytest.mark.parametrize(
    "check",
    [
        {"check": "vibes", "result": "pass", "note": "n"},
        {"check": "confounding", "result": "pass", "note": "n" * 401},
        {"check": "confounding", "result": "pass", "note": "n", "numbers": [_ref()] * 11},
        {"check": "confounding", "result": "concern", "note": "n", "query_ids": ["bad"]},
    ],
)
def test_ut06_06_check_result_field_bounds(check: dict[str, Any]) -> None:
    """UT06-06 CheckResult pattern and length violations are rejected."""
    with pytest.raises(ValidationError):
        CheckResult.model_validate(check)


def test_ut06_06_challenge_defaults_accepted() -> None:
    """UT06-06 model output without round, skeptic_task_id and model validates with defaults."""
    checks = _checks()
    checks[0] = {"check": "confounding", "result": "concern", "note": "n", "query_ids": [_Q1]}
    challenge = Challenge.model_validate(
        {"finding_id": _FND, "checks": checks, "verdict": "revise", "required_actions": ["redo"]}
    )
    assert (challenge.round, challenge.skeptic_task_id, challenge.model) == (0, "", "")
    assert challenge.votes is None
    assert Challenge.model_validate_json(challenge.model_dump_json()) == challenge
    finding = Finding.model_validate(_finding(status="challenged", challenge=[challenge]))
    assert finding.challenge[0].verdict == "revise"


def test_ut06_07_cross_check_length_mismatch_rejected() -> None:
    """UT06-07 CrossCheck with values/query_ids length mismatch is rejected."""
    with pytest.raises(ValidationError):
        CrossCheck(number_id="n1", query_ids=[_Q1, _Q2], values=[1.0], agreed=True)
    with pytest.raises(ValidationError):
        CrossCheck(number_id="n1", query_ids=[_Q1, _Q1], values=[1.0, 1.0], agreed=True)
    with pytest.raises(ValidationError):
        CrossCheck(number_id="x1", query_ids=[_Q1], values=[1.0], agreed=True)
    for bad in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValidationError):
            CrossCheck(number_id="n1", query_ids=[_Q1], values=[bad], agreed=False)
    ok = CrossCheck(number_id="n1", query_ids=[_Q1, _Q2], values=[1.0, 1.01], agreed=True)
    assert ok.query_ids[0] == _Q1


def test_ut06_07_verification_record() -> None:
    """UT06-07 VerificationRecord accepts gate 1 and reason forms; rejects other gates, reasons."""
    cross = CrossCheck(number_id="n1", query_ids=[_Q1, _Q2], values=[2.0, 5.0], agreed=False)
    record = VerificationRecord(
        gate=1, result=_verification(), cross_checks=[cross], reason="crosscheck_disagree:n1"
    )
    assert VerificationRecord.model_validate_json(record.model_dump_json()) == record
    assert VerificationRecord(gate=1, result=_verification(), reason="withdrawn").cross_checks == []
    bad_fields: tuple[dict[str, Any], ...] = (
        {"gate": 2},
        {"reason": "because"},
        {"reason": "withdrawn:"},
        {"reason": "verifier_fail:" + "x" * 200},
    )
    for bad in bad_fields:
        fields = {"gate": 1, "result": _verification()} | bad
        with pytest.raises(ValidationError):
            VerificationRecord.model_validate(fields)
    finding = Finding.model_validate(_finding(status="rejected", verification=record))
    assert finding.verification == record


def test_ut06_93_swarm_task_state_validation_and_round_trip() -> None:
    """UT06-93 duplicates rejected; valid state round-trips through its JSON dump."""
    with pytest.raises(ValidationError):
        SwarmTaskState(phase="running", pending_findings=[_FND, _FND])
    with pytest.raises(ValidationError):
        SwarmTaskState(phase="planning", proposals=[{}] * 6)
    with pytest.raises(ValidationError):
        SwarmTaskState(phase="running", pending_findings=["fnd_x"])
    with pytest.raises(ValidationError):
        SwarmTaskState.model_validate({"phase": "running", "loop": {}})
    state = SwarmTaskState(
        phase="planning",
        pending_findings=[_FND, _FND2],
        proposals=[{"tasks": [], "rationale": "r", "unknowns": []}],
        pseudonyms={"service": {"svc-a": "SERVICE_1"}},
    )
    dumped = state.model_dump(mode="json")
    back = SwarmTaskState.from_envelope({"schema_version": 1, "state": dumped}, task_id=_TASK)
    assert back == state
    assert SwarmTaskState(phase="running").pending_findings == []


def test_ut06_93_read_back_invalid_raises_schema_violation() -> None:
    """UT06-93 a stored invalid state raises SchemaViolation; an absent key reads as None."""
    assert SwarmTaskState.from_envelope({"schema_version": 1, "loop": {}}, task_id=_TASK) is None
    assert SwarmTaskState.from_envelope({"state": None}, task_id=_TASK) is None
    for stored in ("not an object", {"phase": 3}, {"phase": "x", "pending_findings": [_FND] * 2}):
        with pytest.raises(SchemaViolation, match=f"task state invalid: task_id={_TASK}"):
            SwarmTaskState.from_envelope({"state": stored}, task_id=_TASK)


def test_ut06_01_reexports_are_identical() -> None:
    """UT06-01 herness.core.types re-exports the swarm types as the same objects."""
    assert shared.TaskSpec is swarm.TaskSpec
    for name in ("Finding", "Challenge", "SwarmTaskState", "PlannedTask", "SKEPTIC_CHECKS"):
        assert getattr(shared, name) is getattr(swarm, name)
