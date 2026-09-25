"""Spec 05-owned shared types, re-exported as herness.core.types.harness (impl 05 §3.1, R-01).

This file defines nothing; each harness types module adds its import line here.
"""

# Compact layout keeps this file within its 60-line budget (impl 05 §2); names stay sorted.
# fmt: off
from herness.core.types.harness.agent import (  # noqa: I001 - compact re-export layout
    AgentResult, LoopCheckpoint, LoopLimits, LoopSignal, LoopState,
)
from herness.core.types.harness.evidence import (
    Evidence, ItemResult, NumberCheck, NumberRef, UncitedSpan, VerifiableItem,
    VerificationResult,
)
from herness.core.types.harness.llm import (
    LLMRequest, LLMResponse, Message, ReasoningPart, RequestMeta, SystemBlock, TextPart,
    ToolCall, ToolCallPart, ToolResultPart, ToolSpec, Usage,
)
from herness.core.types.harness.tooling import (
    AsyncTool, BudgetLedger, Budgets, OpsHandle, SqlLimits, Tool, ToolContext, ToolErrorInfo,
    ToolResult, TraceEmitter, VectorHandle, VectorHit, WarehouseHandle,
)

__all__: tuple[str, ...] = (
    "AgentResult", "AsyncTool", "BudgetLedger", "Budgets", "Evidence", "ItemResult", "LLMRequest",
    "LLMResponse", "LoopCheckpoint", "LoopLimits", "LoopSignal", "LoopState", "Message",
    "NumberCheck", "NumberRef", "OpsHandle", "ReasoningPart", "RequestMeta", "SqlLimits",
    "SystemBlock", "TextPart", "Tool", "ToolCall", "ToolCallPart", "ToolContext", "ToolErrorInfo",
    "ToolResult", "ToolResultPart", "ToolSpec", "TraceEmitter", "UncitedSpan", "Usage",
    "VectorHandle", "VectorHit", "VerifiableItem", "VerificationResult", "WarehouseHandle",
)
# fmt: on
