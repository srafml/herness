"""Spec 06-owned shared types, re-exported as herness.core.types.swarm (impl 06 §3.1, R-01).

This file defines nothing; each swarm types module adds its import line here.
"""

# Compact layout keeps this file within its 40-line budget (impl 06 §2).
# fmt: off
from herness.core.types.swarm.tasks import (  # noqa: I001 - compact re-export layout
    SKEPTIC_CHECKS, Banner, Challenge, CheckResult, CrossCheck, Depth, EntityScope, Finding,
    FindingStatus, PlannedTask, RejectReason, Role, RunKind, ScopeEntityType, SectionId,
    SkepticCheck, Specialty, SwarmTaskState, TaskBudget, TaskInputs, TaskSpec,
    VerificationRecord,
)

__all__: tuple[str, ...] = (
    "SKEPTIC_CHECKS", "Banner", "Challenge", "CheckResult", "CrossCheck", "Depth", "EntityScope",
    "Finding", "FindingStatus", "PlannedTask", "RejectReason", "Role", "RunKind",
    "ScopeEntityType", "SectionId", "SkepticCheck", "Specialty", "SwarmTaskState", "TaskBudget",
    "TaskInputs", "TaskSpec", "VerificationRecord",
)
# fmt: on
