"""Swarm-provided tools and the per-task tool map (impl 06 U06-60 … U06-64, design 06 §3.6).

Each tool is bound to one task: run, task, session and message ids come from that binding, never
from the model (TH06-17), so no schema has an id field. Every schema is strict (R-26). The
`NumberRef.row_key` travels as `{column, value}` pairs (impl 05 U05-10 fallback: an open object
is not strict-compatible) and becomes the object at this boundary.
"""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable, Mapping
from typing import Final, cast, get_args

from pydantic import JsonValue, ValidationError

from herness.core.errors import ConfigError, ToolInputError
from herness.core.types import (
    AsyncTool,
    Finding,
    FindingStatus,
    Role,
    ScopeEntityType,
    Specialty,
    TaskSpec,
    Tool,
    ToolContext,
    ToolResult,
)
from herness.harness.blackboard import Blackboard, FindingFilter
from herness.harness.swarm.spawn import SpawnBroker
from herness.harness.tools import NUMBER_REF_SCHEMA, TOOL_CONTENT_MAX_CHARS

__all__ = ["EscalateTool", "ListFindingsTool", "PostFindingTool", "RequestSubtaskTool"]
__all__ += ["build_task_tools"]

QUERY_ID_PATTERN: Final = r"^q_[0-9a-f]{16}$"
_QID: Final[JsonValue] = {"type": "string", "pattern": QUERY_ID_PATTERN}
_NOTE_RESERVE: Final = 80  # room for the truncation note line
type _Schema = dict[str, JsonValue]
type PastReader = Callable[..., list[Finding]]
type Escalate = Callable[[str, str], Awaitable[tuple[str, str]]]
type _AnyTool = Tool | AsyncTool


def _enum(values: tuple[object, ...], *, nullable: bool = False) -> _Schema:
    items: list[JsonValue] = [str(v) for v in values]
    if nullable:
        return {"type": ["string", "null"], "enum": [*items, None]}
    return {"type": "string", "enum": items}


def _text(lo: int, hi: int) -> _Schema:
    return {"type": "string", "minLength": lo, "maxLength": hi}


def _obj(props: dict[str, JsonValue]) -> _Schema:
    return {
        "type": "object",
        "properties": props,
        "required": list(props),
        "additionalProperties": False,
    }


_ENTITY_TYPES: Final = get_args(ScopeEntityType.__value__)
_ENTITY_ID: Final = _text(1, 200)
POST_FINDING_SCHEMA: Final = _obj(
    {
        "claim": _text(1, 1_500),
        "entity_type": _enum(_ENTITY_TYPES),
        "entity_id": _ENTITY_ID,
        "numbers": {"type": "array", "minItems": 1, "maxItems": 20, "items": NUMBER_REF_SCHEMA},
        "query_ids": {"type": "array", "maxItems": 50, "items": _QID},
        "confidence": {"type": "number", "minimum": 0, "maximum": 1},
    }
)


def _nullable_array(items: _Schema, max_items: int | None = None) -> _Schema:
    bound: _Schema = {} if max_items is None else {"maxItems": max_items}
    return {"type": ["array", "null"], "items": items, **bound}


LIST_FINDINGS_SCHEMA: Final = _obj(
    {
        "status": _nullable_array(_enum(get_args(FindingStatus.__value__))),
        "entity_type": _enum(_ENTITY_TYPES, nullable=True),
        "entity_ids": _nullable_array(_ENTITY_ID, 50),
        "task_ids": _nullable_array(_text(1, 64), 200),
        "author_roles": _nullable_array(_enum(get_args(Role.__value__))),
        "min_confidence": {"type": ["number", "null"], "minimum": 0, "maximum": 1},
        "include_superseded": {"type": ["boolean", "null"]},
        "limit": {"type": ["integer", "null"], "minimum": 1, "maximum": 500},
    }
)
REQUEST_SUBTASK_SCHEMA: Final = _obj(
    {
        "objective": _text(1, 2_000),
        "specialty": _enum(get_args(Specialty.__value__)),
        "entity_type": _enum(_ENTITY_TYPES),
        "entity_ids": {"type": "array", "minItems": 1, "maxItems": 50, "items": _ENTITY_ID},
        "reason": _text(1, 1_500),
    }
)
ESCALATE_SCHEMA: Final = _obj({"question": _text(1, 2_000), "reason": _text(1, 500)})
_ESC: Final = {"question", "reason"}


def _row_key(pairs: JsonValue) -> JsonValue:
    """`[{column, value}, …]` → `{column: value}`; anything else is left to validation."""
    if not isinstance(pairs, list):
        return pairs
    out: dict[str, JsonValue] = {}
    for pair in pairs:
        if not isinstance(pair, dict) or not isinstance(column := pair.get("column"), str):
            return pairs
        if column in out:
            msg = f"duplicate row_key column {column[:128]}"
            raise ToolInputError(msg)
        out[column] = pair.get("value")
    return out


def _numbers(value: JsonValue) -> JsonValue:
    if not isinstance(value, list):
        return value
    return [
        {**n, "row_key": _row_key(n["row_key"])} if isinstance(n, dict) and "row_key" in n else n
        for n in value
    ]


def _finding_query_ids(args: Mapping[str, JsonValue]) -> list[str]:
    """The committed finding's `query_ids`: the given ones plus every number's `query_id`."""
    numbers = cast("list[dict[str, JsonValue]]", args["numbers"])  # valid: the post succeeded
    return sorted({*cast("list[str]", args["query_ids"]), *(str(n["query_id"]) for n in numbers)})


class PostFindingTool:
    """`post_finding` bound to one task; backend `Blackboard.post` (U06-60, R-27)."""

    name: str = "post_finding"
    description: str = "Post one finding; every number is a [[nX]] marker with a cited NumberRef."
    input_schema: _Schema = POST_FINDING_SCHEMA

    def __init__(self, bb: Blackboard) -> None:
        self._bb = bb

    def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        """Commit the finding; a rejected post raises `ToolInputError` (dispatch reports it)."""
        args = {**kwargs, "numbers": _numbers(kwargs.get("numbers"))}  # absent: rejected by post
        fid = self._bb.post(ctx, **args)
        return ToolResult(
            ok=True,
            content=f"finding_id={fid} status=proposed",
            data={"finding_id": fid},
            finding_ids=[fid],
            query_ids=_finding_query_ids(args),
        )


def _one_line(text: str) -> str:
    return text.replace("\r\n", " ").replace("\r", " ").replace("\n", " ")


def _block(f: Finding) -> str:
    head = f"{f.finding_id} | {f.status} | {f.entity_type}:{f.entity_id} | {f.confidence:.2f}"
    refs = "; ".join(f"{n.id}={n.value} {n.unit} ({n.query_id}, {n.column})" for n in f.numbers)
    return f"{head} | {_one_line(f.claim)}\nnumbers: {refs}"


def render_findings(findings: list[Finding]) -> ToolResult:
    """U06-61 step 3: blocks until the next would pass 12,000 chars; ids of what was shown."""
    blocks: list[str] = []
    size, limit = 0, TOOL_CONTENT_MAX_CHARS - _NOTE_RESERVE
    for f in findings:
        block = _block(f)
        if size + len(block) + 1 > limit:
            break
        blocks.append(block)
        size += len(block) + 1
    shown = findings[: len(blocks)]
    truncated = len(shown) < len(findings)
    if truncated:
        blocks.append(f"truncated: {len(shown)} of {len(findings)} findings shown")
    query_ids = list(dict.fromkeys(q for f in shown for q in f.query_ids))
    return ToolResult(
        ok=True,
        content="\n".join(blocks) if findings else "no findings",
        finding_ids=[f.finding_id for f in shown],
        query_ids=query_ids,
        truncated=truncated,
    )


class ListFindingsTool:
    """`list_findings`: the bound run's findings, or past verified findings in chat (U06-61)."""

    name: str = "list_findings"
    description: str = "List committed findings; a null filter field means no filter."
    input_schema: _Schema = LIST_FINDINGS_SCHEMA

    def __init__(
        self, *, bb: Blackboard | None, run_id: str | None, past_reader: PastReader | None
    ) -> None:
        if (bb is None or run_id is None) and past_reader is None:
            msg = "tool list_findings has no backend"
            raise ConfigError(msg)
        self._bb, self._run_id, self._past = bb, run_id, past_reader

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        """Read with the bound run id (the model never names a run, TH06-17)."""
        del ctx
        props = LIST_FINDINGS_SCHEMA["properties"]
        if not isinstance(props, dict) or set(kwargs) - set(props):
            msg = "unexpected list_findings arguments"
            raise ToolInputError(msg)
        given = {k: v for k, v in kwargs.items() if v is not None}
        if self._bb is not None and self._run_id is not None:
            try:
                flt = FindingFilter.model_validate({**given, "run_id": self._run_id})
            except ValidationError:
                msg = "invalid list_findings arguments"
                raise ToolInputError(msg) from None
            return render_findings(await self._bb.list_findings(flt))
        reader = self._past
        assert reader is not None  # noqa: S101 - __init__ guarantees a backend
        limit = given.get("limit", 500)
        found = reader(
            entity_type=given.get("entity_type"), entity_ids=given.get("entity_ids"), limit=limit
        )
        return render_findings(found)


def _json_result(payload: dict[str, JsonValue]) -> ToolResult:
    return ToolResult(ok=True, content=json.dumps(payload, separators=(",", ":")), data=payload)


class RequestSubtaskTool:
    """`request_subtask` bound to the parent task; a denial is information (U06-62)."""

    name: str = "request_subtask"
    description: str = "Ask for a follow-up analyst task; returns at once, approved or not."
    input_schema: _Schema = REQUEST_SUBTASK_SCHEMA

    def __init__(self, broker: SpawnBroker, parent: TaskSpec) -> None:
        self._broker, self._parent = broker, parent

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        """Never waits for the child."""
        del ctx
        d = await self._broker.request(self._parent, kwargs)
        if d.approved:
            return _json_result({"approved": True, "task_id": d.task_id})
        return _json_result({"approved": False, "reason": d.reason})


class EscalateTool:
    """`escalate` for chat; session and message come from the bound callable (U06-63)."""

    name: str = "escalate"
    description: str = "Start a review run for a question that needs one; returns its ids."
    input_schema: _Schema = ESCALATE_SCHEMA

    def __init__(self, escalate: Escalate) -> None:
        self._escalate = escalate

    async def __call__(self, ctx: ToolContext, **kwargs: JsonValue) -> ToolResult:
        """The bound callable is idempotent per message (U06-134)."""
        del ctx
        question, reason = kwargs.get("question"), kwargs.get("reason")
        if not isinstance(question, str) or not isinstance(reason, str) or set(kwargs) != _ESC:
            msg = "escalate needs exactly question and reason strings"
            raise ToolInputError(msg)
        run_id, job_id = await self._escalate(question, reason)
        return _json_result({"run_id": run_id, "job_id": job_id})


def _list_tool(spec: TaskSpec, bb: Blackboard | None, past: PastReader | None) -> _AnyTool | None:
    """Chat roles read past verified findings; review roles the bound run's findings."""
    if spec.role == "chat":
        return None if past is None else ListFindingsTool(bb=None, run_id=None, past_reader=past)
    return None if bb is None else ListFindingsTool(bb=bb, run_id=spec.run_id, past_reader=None)


def build_task_tools(
    spec: TaskSpec,
    *,
    bb: Blackboard | None,
    broker: SpawnBroker | None,
    escalate: Escalate | None = None,
    past_reader: PastReader | None = None,
) -> dict[str, _AnyTool]:
    """`ToolContext.task_tools` of one task: its swarm tools, each bound to it (U06-64)."""
    builders: dict[str, Callable[[], _AnyTool | None]] = {
        "post_finding": lambda: None if bb is None else PostFindingTool(bb),
        "list_findings": lambda: _list_tool(spec, bb, past_reader),
        "request_subtask": lambda: None if broker is None else RequestSubtaskTool(broker, spec),
        "escalate": lambda: None if escalate is None else EscalateTool(escalate),
    }
    tools: dict[str, _AnyTool] = {}
    for name in sorted(builders.keys() & set(spec.tools)):
        tool = builders[name]()
        if tool is None:
            msg = f"tool {name} has no backend"
            raise ConfigError(msg)
        tools[name] = tool
    return tools
