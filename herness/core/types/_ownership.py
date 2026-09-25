"""Ownership tables of the shared-types package (impl 00 U00-45, U00-46; R-01, R-02).

Read by tools/check_type_ownership.py with runpy. Imports only types and typing.
"""

import types
from typing import Final

# fmt: off
OWNER_MODULES: Final = types.MappingProxyType(
    {"03": "decisions", "05": "harness", "06": "swarm", "07": "memory", "08": "jobs",
     "09": "reports"}
)
OWNER_IMPORTS: Final = types.MappingProxyType(
    {"03": frozenset[str](), "05": frozenset[str](), "06": frozenset({"harness", "jobs"}),
     "07": frozenset({"harness", "swarm"}), "08": frozenset({"harness"}),
     "09": frozenset[str]()}
)
_NAMES: Final = {
    "03": ("QuestionType", "Entity", "Question", "QuestionSet", "DecisionInput", "Answer",
           "DecisionOutput"),
    "05": ("TextPart", "ToolCall", "ToolCallPart", "ToolResultPart", "ReasoningPart", "Message",
           "SystemBlock", "ToolSpec", "RequestMeta", "LLMRequest", "Usage", "LLMResponse",
           "Tool", "AsyncTool", "ToolErrorInfo", "ToolResult", "SqlLimits", "Budgets",
           "BudgetLedger", "TraceEmitter", "WarehouseHandle", "OpsHandle", "VectorHandle",
           "VectorHit", "ToolContext", "NumberRef", "Evidence", "NumberCheck", "UncitedSpan",
           "ItemResult", "VerificationResult", "VerifiableItem", "LoopSignal", "LoopLimits",
           "LoopState", "LoopCheckpoint", "AgentResult"),
    "06": ("RunKind", "Depth", "Role", "Specialty", "ScopeEntityType", "SkepticCheck",
           "SKEPTIC_CHECKS", "RejectReason", "FindingStatus", "Banner", "SectionId",
           "EntityScope", "TaskInputs", "TaskBudget", "TaskSpec", "PlannedTask", "Finding",
           "CheckResult", "Challenge", "CrossCheck", "VerificationRecord", "SwarmTaskState",
           "Paragraph", "Section", "RecommendationItem", "RankedEntity", "Coverage",
           "ReportDraft", "ChatAnswer", "ChatEvent"),
    "07": ("Layer", "Kind", "Status", "KIND_LAYER", "Provenance", "MemoryItem",
           "MemoryProposal", "RecallHit", "MemoryRunContext", "RecommendationDraft",
           "PriorRecommendation", "PriorContext", "ConfidenceAdjustment", "SimilarOutcome"),
    "08": ("GpuClass", "JobKind", "ServiceName", "ChatMode", "BreakerState", "PolicyName",
           "JobSpec", "JobOutcome", "MetricSample"),
    "09": ("ReportManifest",),
}
TYPE_OWNERS: Final = types.MappingProxyType(
    {name: owner for owner, names in _NAMES.items() for name in names}
)
DECLARED_ELSEWHERE: Final = types.MappingProxyType(
    {
        "LoopHooks": ("05", "herness.harness.loop"),
        "HarnessHooks": ("05", "herness.harness.hooks"),
        "GatedClient": ("05", "herness.harness.hooks"),
        "Tracer": ("05", "herness.harness.tracing"),
        "RunBudget": ("06", "herness.harness.budget"),
        "ModelChain": ("08", "herness.core.resilience"),
        "loop_signal_policy": ("08", "herness.core.resilience"),
        "JobContext": ("08", "herness.core.jobs"),
    }
)
# fmt: on
