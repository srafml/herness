"""Spec 06-owned shared types, re-exported as herness.core.types.swarm (impl 06 §3.1, R-01).

This file defines nothing; each swarm types module adds its import line here.
"""

# Compact layout keeps this file within its 40-line budget (impl 06 §2).
# fmt: off
from herness.core.types.swarm.drafts import (  # noqa: I001 - compact re-export layout
    CHAT_EVENT_ADAPTER, ChatAnswer, ChatEvent, CorrectionCapturedEvent, Coverage, ErrorEvent,
    EscalatedEvent, EvidenceEvent, FinalEvent, ModeEvent, Paragraph, RankedEntity,
    RecommendationItem, ReportDraft, Section, TokenEvent, ToolEvent, VerificationEvent,
)
from herness.core.types.swarm.tasks import (
    SKEPTIC_CHECKS, Banner, Challenge, CheckResult, CrossCheck, Depth, EntityScope, Finding,
    FindingStatus, PlannedTask, RejectReason, Role, RunKind, ScopeEntityType, SectionId,
    SkepticCheck, Specialty, SwarmTaskState, TaskBudget, TaskInputs, TaskSpec,
    VerificationRecord,
)

__all__: tuple[str, ...] = (
    "CHAT_EVENT_ADAPTER", "SKEPTIC_CHECKS", "Banner", "Challenge", "ChatAnswer", "ChatEvent",
    "CheckResult", "CorrectionCapturedEvent", "Coverage", "CrossCheck", "Depth", "EntityScope",
    "ErrorEvent", "EscalatedEvent", "EvidenceEvent", "FinalEvent", "Finding", "FindingStatus",
    "ModeEvent", "Paragraph", "PlannedTask", "RankedEntity", "RecommendationItem",
    "RejectReason", "ReportDraft", "Role", "RunKind", "ScopeEntityType", "Section", "SectionId",
    "SkepticCheck", "Specialty", "SwarmTaskState", "TaskBudget", "TaskInputs", "TaskSpec",
    "TokenEvent", "ToolEvent", "VerificationEvent", "VerificationRecord",
)
# fmt: on
