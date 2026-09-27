"""Model-call plumbing of ``summarize_notes`` (impl 07 U07-77 steps 2-3, U07-99).

Size-forced private sibling of ``compactor.py`` (T07-14); only that module imports it. The
dropped groups reach the model only escaped and wrapped as ``tool_results`` data (R-20).
"""

import asyncio
import functools
from collections.abc import Sequence
from importlib import resources
from typing import Final, cast

from pydantic import JsonValue

from herness.core.errors import ConfigError, ModelRefused
from herness.core.ids import sha256_hex
from herness.core.types import (
    LLMRequest,
    LLMResponse,
    Message,
    RequestMeta,
    SystemBlock,
    TextPart,
    ToolContext,
)
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.llm.tokens import estimate_tokens
from herness.harness.memory.compact_build import Group, transcript
from herness.harness.memory.render import escape_content, wrap_untrusted
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.working import CompactionNotes, Scratchpad
from herness.harness.tracing import llm_call_fields

__all__ = ["NOTES_SCHEMA", "PROMPT_FILE", "Completer", "chunks", "compaction_prompt", "request"]

PROMPT_FILE: Final = "prompts/compaction_notes.md"
REQUEST_KEY_CHARS: Final = 200


@functools.cache
def compaction_prompt() -> tuple[str, str]:
    """The U07-99 system prompt and its 16-hex content hash; read once per process."""
    try:
        text = resources.files("herness.harness.memory").joinpath(PROMPT_FILE).read_text("utf-8")
    except OSError as exc:
        msg = f"prompt file {PROMPT_FILE} missing"
        raise ConfigError(msg) from exc
    return text, sha256_hex(text)[:16]


def _notes_schema() -> dict[str, JsonValue]:
    schema: dict[str, JsonValue] = CompactionNotes.model_json_schema()
    properties = cast("dict[str, JsonValue]", schema["properties"])
    del properties["steps"]  # steps are deterministic only (U07-73 step 4)
    return schema


NOTES_SCHEMA: Final = _notes_schema()


class Completer:
    """``complete_validated``'s client: timeout, run-ledger charge and trace per response.

    Every response, the repair one included, is charged to ``ctx.ledger`` (its budget error
    propagates, R-25) and traced as ``llm_call`` with the prompt's content hash. A refusal
    raises ``ModelRefused`` so the caller falls back to deterministic notes. A client without
    sampling parameters gets ``temperature=None`` on every request, the repair one included
    (spec 08 ``build_repair_request`` always sets 0.0).
    """

    def __init__(self, client: LLMClient, ctx: ToolContext, profile: ClientConfig) -> None:
        self.name = client.name
        self._client, self._ctx, self._profile = client, ctx, profile

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One call under ``asyncio.wait_for(profile.timeout_s)``."""
        if not self._profile.supports.sampling_params and req.temperature is not None:
            req = req.model_copy(update={"temperature": None})
        resp = await asyncio.wait_for(self._client.acomplete(req), self._profile.timeout_s)
        usage = resp.usage
        self._ctx.ledger.charge(usage.prompt_total(), usage.output_tokens, resp.cost_usd)
        fields, payload = llm_call_fields(
            req, resp, prompt_hash=compaction_prompt()[1], gate_wait_ms=0
        )
        step = req.metadata.step
        self._ctx.tracer.emit("llm_call", role=self._ctx.role, step=step, payload=payload, **fields)
        if resp.stop_reason == "refusal":
            msg = "model refused the compaction notes"
            raise ModelRefused(msg, category=resp.refusal_category, client=self.name)
        return resp


def _est(text: str) -> int:
    return estimate_tokens((), (), [SystemBlock(text=text)])


def chunks(groups: Sequence[Group], messages: Sequence[Message], budget: int) -> list[str]:
    """Transcripts of ``groups`` cut at group boundaries to at most half the budget each."""
    limit = budget // 2
    out: list[str] = []
    current: list[Group] = []
    for group in groups:
        if current and _est(transcript([*current, group], messages)) > limit:
            out.append(transcript(current, messages))
            current = []
        current.append(group)
    out.append(transcript(current, messages))
    return [text[: limit * 7 // 2] for text in out]  # one oversize group is cut (LLM10)


def _head(prior: CompactionNotes | None, scratchpad: Scratchpad) -> str:
    notes = "none" if prior is None else prior.model_dump_json(exclude={"steps"})
    ids = [ref.id for ref in scratchpad.cited_numbers()] + sorted(scratchpad.query_ids())
    return f"PRIOR NOTES:\n{escape_content(notes)}\n\nLEDGER IDS:\n{', '.join(ids)}\n\n"


def request(
    profile: ClientConfig,
    cfg: CompactionConfig,
    ctx: ToolContext,
    notes: tuple[CompactionNotes | None, Scratchpad],
    chunk: tuple[int, str],
    step: int,
) -> LLMRequest:
    """The notes request for chunk ``(index, text)`` given ``(prior notes, scratchpad)``."""
    prior, scratchpad = notes
    index, text = chunk
    body = _head(prior, scratchpad) + wrap_untrusted("tool_results", None, escape_content(text))
    key = f"compaction:{ctx.task_id}:{scratchpad.compactions}:{index}"[:REQUEST_KEY_CHARS]
    meta = RequestMeta(
        run_id=ctx.run_id,
        task_id=ctx.task_id,
        role=ctx.role,
        model_role=ctx.role,
        step=step,
        request_key=key,
    )
    return LLMRequest(
        client=profile.name,
        system=[SystemBlock(text=compaction_prompt()[0])],
        messages=[Message(role="user", parts=[TextPart(text=body)])],
        response_schema=NOTES_SCHEMA,
        response_schema_name="compaction_notes",
        max_output_tokens=min(cfg.summary_max_tokens, profile.max_output_tokens),
        temperature=0.0 if profile.supports.sampling_params else None,
        tools=[],
        timeout_s=profile.timeout_s,
        metadata=meta,
    )
