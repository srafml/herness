"""Tests for the shared-types package and its ownership tables (U00-44, U00-45, U00-46)."""

import ast
from pathlib import Path

import pytest

import herness.core.types as shared
from herness.core.types import _ownership as own

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]

EXPECTED = {
    "03": {
        "QuestionType",
        "Entity",
        "Question",
        "QuestionSet",
        "DecisionInput",
        "Answer",
        "DecisionOutput",
    },
    "05": {
        "TextPart",
        "ToolCall",
        "ToolCallPart",
        "ToolResultPart",
        "ReasoningPart",
        "Message",
        "SystemBlock",
        "ToolSpec",
        "RequestMeta",
        "LLMRequest",
        "Usage",
        "LLMResponse",
        "Tool",
        "AsyncTool",
        "ToolErrorInfo",
        "ToolResult",
        "SqlLimits",
        "Budgets",
        "BudgetLedger",
        "TraceEmitter",
        "WarehouseHandle",
        "OpsHandle",
        "VectorHandle",
        "VectorHit",
        "ToolContext",
        "NumberRef",
        "Evidence",
        "NumberCheck",
        "UncitedSpan",
        "ItemResult",
        "VerificationResult",
        "VerifiableItem",
        "LoopSignal",
        "LoopLimits",
        "LoopState",
        "LoopCheckpoint",
        "AgentResult",
    },
    "06": {
        "RunKind",
        "Depth",
        "Role",
        "Specialty",
        "ScopeEntityType",
        "SkepticCheck",
        "SKEPTIC_CHECKS",
        "RejectReason",
        "FindingStatus",
        "Banner",
        "SectionId",
        "EntityScope",
        "TaskInputs",
        "TaskBudget",
        "TaskSpec",
        "PlannedTask",
        "Finding",
        "CheckResult",
        "Challenge",
        "CrossCheck",
        "VerificationRecord",
        "SwarmTaskState",
        "Paragraph",
        "Section",
        "RecommendationItem",
        "RankedEntity",
        "Coverage",
        "ReportDraft",
        "ChatAnswer",
        "ChatEvent",
    },
    "07": {
        "Layer",
        "Kind",
        "Status",
        "KIND_LAYER",
        "Provenance",
        "MemoryItem",
        "MemoryProposal",
        "RecallHit",
        "MemoryRunContext",
        "RecommendationDraft",
        "PriorRecommendation",
        "PriorContext",
        "ConfidenceAdjustment",
        "SimilarOutcome",
    },
    "08": {
        "GpuClass",
        "JobKind",
        "ServiceName",
        "ChatMode",
        "BreakerState",
        "PolicyName",
        "JobSpec",
        "JobOutcome",
        "MetricSample",
    },
    "09": {"ReportManifest"},
}


def test_ut00_48_init_reexports_only() -> None:
    """UT00-48 __all__ equals the owner names of existing submodules; no definitions."""
    present = {
        owner
        for owner, sub in own.OWNER_MODULES.items()
        if (ROOT / "herness/core/types" / f"{sub}.py").exists()
        or (ROOT / "herness/core/types" / sub / "__init__.py").exists()
    }
    expected = sorted(n for n, o in own.TYPE_OWNERS.items() if o in present)
    assert list(shared.__all__) == expected
    tree = ast.parse((ROOT / "herness/core/types/__init__.py").read_text(encoding="utf-8"))
    for node in tree.body:
        assert not isinstance(node, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
        if isinstance(node, ast.Assign | ast.AnnAssign):
            target = node.targets[0] if isinstance(node, ast.Assign) else node.target
            assert isinstance(target, ast.Name)
            assert target.id == "__all__"


def test_ut00_80_ownership_tables() -> None:
    """UT00-80 names per owner, no duplicates, exclusions and an acyclic import graph."""
    by_owner: dict[str, set[str]] = {}
    for name, owner in own.TYPE_OWNERS.items():
        by_owner.setdefault(owner, set()).add(name)
    assert by_owner == EXPECTED
    assert not set(own.TYPE_OWNERS) & set(own.DECLARED_ELSEWHERE)
    assert "impact_usd" not in own.TYPE_OWNERS
    assert "JobContext" not in own.TYPE_OWNERS
    assert own.OWNER_IMPORTS["06"] == frozenset({"harness", "jobs"})
    subs = {sub: owner for owner, sub in own.OWNER_MODULES.items()}
    visiting: set[str] = set()

    def visit(owner: str) -> None:
        assert owner not in visiting, "cycle"
        visiting.add(owner)
        for sub in own.OWNER_IMPORTS[owner]:
            visit(subs[sub])
        visiting.discard(owner)

    for owner in own.OWNER_MODULES:
        visit(owner)
