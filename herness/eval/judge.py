"""LLM rubric judge with a SHA-256 keyed file cache (design 11 §5.3.2; U11-61, U11-76).

Prose only: the redacted, escaped final text sits in the R-20 `<untrusted_data>` block of
`prompts/judge.md`; a reply off the `judge_scores` schema gets one repair call, then
`ModelUnavailable`. `EgressBlocked` from inside an off-network client propagates (TH11-04,
TH11-06). Errors and trace fields carry ids and reason codes, never answer text.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import re
import tempfile
from collections.abc import Callable, Sequence
from importlib import resources
from pathlib import Path
from typing import Any, Final, cast

from pydantic import BaseModel, ConfigDict, JsonValue

from herness.core.errors import ModelUnavailable, OutputValidationError
from herness.core.types import LLMRequest, LLMResponse, Message, RequestMeta, TextPart
from herness.harness.llm.base import LLMClient
from herness.harness.llm.settings import ClientConfig
from herness.harness.tracing import Tracer, llm_call_fields

__all__ = ["JudgeScore", "RubricJudge", "cache_key", "judge_prompt", "judge_prompt_hash"]

_ROLE, _MODEL_ROLE, _SCHEMA_NAME = "judge_eval", "eval_judge", "judge_scores"
_ASK: Final = "Score the answer."
_REPAIR: Final = "The reply failed the judge_scores schema ({reason}); reply with valid JSON."
_OPEN: Final = '<untrusted_data source="eval_answer" record_id="">'
_CLOSE: Final = "</untrusted_data>"
_SLOT_RE: Final = re.compile(r"\{\{(criteria|answer)\}\}")
_NO_RUN: Final = "run_" + "0" * 26  # the null tracer's run id, used when no tracer is given
_MIN, _MAX, _RATIONALE_MAX, _MAX_OUTPUT_TOKENS = 1, 5, 500, 1_024


class JudgeScore(BaseModel):
    """One judge verdict; `cached` is true when it came from the cache (U11-61)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    scores: dict[str, int]
    rationale: str
    model: str
    cached: bool


@functools.cache
def judge_prompt() -> str:
    """The shipped `prompts/judge.md` template (U11-76)."""
    return resources.files("herness.eval").joinpath("prompts/judge.md").read_text(encoding="utf-8")


@functools.cache
def judge_prompt_hash() -> str:
    """16 hex of SHA-256 over the prompt file: the prompt version for `summary.json`."""
    raw = resources.files("herness.eval").joinpath("prompts/judge.md").read_bytes()
    return hashlib.sha256(raw).hexdigest()[:16]


def cache_key(
    model: str, question_id: str, criteria: Sequence[str], min_score: float, final_text: str
) -> str:
    """SHA-256 hex of the canonical JSON of (model, rubric, question id, final text)."""
    body = {
        "model": model,
        "rubric": {"criteria": list(criteria), "min_score": min_score},
        "question_id": question_id,
        "text": final_text,
    }
    canonical = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _render(criteria: Sequence[str], redacted: str) -> str:
    """Escape `&`, `<`, `>` so no literal `</untrusted_data` survives (R-20), then fill slots."""
    escaped = redacted.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    slots = {
        "criteria": "\n".join(f"- {name}" for name in criteria),
        "answer": f"{_OPEN}\n{escaped}\n{_CLOSE}",
    }
    return _SLOT_RE.sub(lambda m: slots[m.group(1)], judge_prompt())


def _schema(criteria: Sequence[str]) -> dict[str, JsonValue]:
    base: dict[str, JsonValue] = {"type": "object", "additionalProperties": False}
    score: dict[str, JsonValue] = {"type": "integer", "minimum": _MIN, "maximum": _MAX}
    scores: dict[str, JsonValue] = {name: dict(score) for name in criteria}
    rationale: dict[str, JsonValue] = {"type": "string", "maxLength": _RATIONALE_MAX}
    props: dict[str, JsonValue] = {
        "scores": {**base, "properties": scores, "required": list(criteria)},
        "rationale": rationale,
    }
    return {**base, "properties": props, "required": ["scores", "rationale"]}


def _parsed(resp: LLMResponse) -> object:
    if resp.parsed is not None:
        return resp.parsed
    with contextlib.suppress(ValueError):
        return json.loads(resp.text)
    return None  # not JSON: reported as `not_object`


def _problem(raw: object, criteria: Sequence[str]) -> str | None:
    """The reason code of an invalid reply, or None; score values are checked per criterion."""
    if not isinstance(raw, dict):
        return "not_object"
    scores, rationale = raw.get("scores"), raw.get("rationale")
    if not isinstance(scores, dict):
        return "scores_not_object"
    if not isinstance(rationale, str) or len(rationale) > _RATIONALE_MAX:
        return "bad_rationale"
    values = [scores.get(name) for name in criteria]
    if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
        return "missing_criterion"
    return None if all(_MIN <= cast("int", v) <= _MAX for v in values) else "out_of_range"


def _validate(raw: object, criteria: Sequence[str]) -> tuple[dict[str, int], str]:
    """Scores of every criterion (extras dropped) and the rationale, or OutputValidationError."""
    reason = _problem(raw, criteria)
    if reason is not None:
        msg = "judge reply failed validation"
        raise OutputValidationError(msg, reason=reason)
    data = cast("dict[str, Any]", raw)
    return {name: data["scores"][name] for name in criteria}, data["rationale"]


class RubricJudge:
    """LLM rubric judge with a SHA-256 keyed cache (U11-61)."""

    def __init__(
        self,
        client: LLMClient,
        client_cfg: ClientConfig,
        *,
        cache_dir: Path,
        temperature: float,
        tracer: Tracer | None,
        redact: Callable[[str], str | None],
    ) -> None:
        self._client, self._cfg = client, client_cfg
        self._cache_dir, self._temperature = Path(cache_dir), temperature
        self._tracer, self._redact = tracer, redact

    def score(
        self, question_id: str, criteria: Sequence[str], min_score: float, final_text: str
    ) -> JudgeScore:
        """Score `final_text` against `criteria`: cache hit, else one call plus one repair."""
        criteria = list(criteria)
        key = cache_key(self._cfg.model, question_id, criteria, min_score, final_text)
        path = self._cache_dir / key[:2] / f"{key}.json"
        hit = self._read_cache(path, criteria)
        if hit is not None:
            return hit
        redacted = self._redact(final_text)
        if redacted is None:  # fail closed: unredacted text never reaches the client
            msg = "judge answer redaction failed"
            raise ModelUnavailable(msg, question_id=question_id, reason="redaction_failed")
        base = self._request(question_id, criteria, redacted, key)
        try:
            result = self._attempt(base, criteria)
        except OutputValidationError as first:
            try:
                result = self._attempt(self._repair(base, first), criteria)
            except OutputValidationError as second:
                msg = "judge reply invalid after one repair"
                raise ModelUnavailable(msg, question_id=question_id) from second
        self._write_cache(path, result)
        return result

    def _request(
        self, question_id: str, criteria: Sequence[str], redacted: str, key: str
    ) -> LLMRequest:
        tracer = self._tracer
        meta = RequestMeta(
            run_id=_NO_RUN if tracer is None else tracer.run_id,
            task_id=None,
            role=_ROLE,
            model_role=_MODEL_ROLE,
            step=0,
            request_key=f"judge:{question_id}:{key[:8]}",
        )
        return LLMRequest.model_validate(
            {
                "client": self._cfg.name,
                "system": [{"text": _render(criteria, redacted)}],
                "messages": [Message(role="user", parts=[TextPart(text=_ASK)])],
                "response_schema": _schema(criteria),
                "response_schema_name": _SCHEMA_NAME,
                "max_output_tokens": min(self._cfg.max_output_tokens, _MAX_OUTPUT_TOKENS),
                "temperature": self._temperature,
                "timeout_s": self._cfg.timeout_s,
                "metadata": meta,
            }
        )

    @staticmethod
    def _repair(base: LLMRequest, error: OutputValidationError) -> LLMRequest:
        reason = str(error.context.get("reason") or "invalid")
        note = Message(role="user", parts=[TextPart(text=_REPAIR.format(reason=reason))])
        meta = base.metadata.model_copy(update={"step": 1})
        return base.model_copy(update={"messages": [*base.messages, note], "metadata": meta})

    def _attempt(self, req: LLMRequest, criteria: Sequence[str]) -> JudgeScore:
        """One client call, its `llm_call` trace event, then validation of the reply."""
        resp = self._client.complete(req)
        if self._tracer is not None:
            f, p = llm_call_fields(req, resp, prompt_hash=judge_prompt_hash(), gate_wait_ms=0)
            extra: dict[str, Any] = f
            self._tracer.emit("llm_call", role=_ROLE, step=req.metadata.step, payload=p, **extra)
        scores, rationale = _validate(_parsed(resp), criteria)
        return JudgeScore(scores=scores, rationale=rationale, model=resp.model, cached=False)

    @staticmethod
    def _read_cache(path: Path, criteria: Sequence[str]) -> JudgeScore | None:
        """The cached verdict; a missing, unreadable or invalid file is a miss."""
        try:  # pydantic's ValidationError (for example a non-str model) is a ValueError
            raw = json.loads(path.read_text(encoding="utf-8"))
            scores, rationale = _validate(raw, criteria)
            return JudgeScore(scores=scores, rationale=rationale, model=raw["model"], cached=True)
        except (OSError, ValueError, KeyError, OutputValidationError):
            return None

    @staticmethod
    def _write_cache(path: Path, result: JudgeScore) -> None:
        """Write atomically: a temp file in the same directory, then `os.replace`."""
        path.parent.mkdir(parents=True, exist_ok=True)
        body = result.model_dump(mode="json", exclude={"cached"})
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.stem[:8]}.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(body, handle, sort_keys=True)
            os.replace(tmp, path)
        except OSError:  # a concurrent writer holds the target (Windows); its content is equal
            if not path.is_file():
                raise
        finally:
            with contextlib.suppress(FileNotFoundError):
                os.unlink(tmp)
