"""LLM decider: k-vote structured-output classifier (impl 03 U03-60 ... U03-64; design 03 §3.3).

Each vote is one `complete_validated` call (enum-constrained `enrich_votes` schema, at most
2 repairs) under `aretry_call("llm_local", breaker_key="decider:llm")`; a vote still invalid
after its repairs is dropped, and the Laplace-smoothed vote counts are the distribution.
The ticket text sits in one R-20 `<untrusted_data>` block whose closing delimiter the text
cannot forge (TH03-01). The client is a local structural protocol, so enrichment never
imports `herness.harness` (R-05). Prompts are files under `herness/enrich/prompts/`.
Logs carry record ids, counts and error classes only. Registration is T03-16's.
"""

from __future__ import annotations

import asyncio
import html
import json
import re
from collections.abc import Sequence
from pathlib import Path
from typing import Annotated, Final, Protocol, cast, runtime_checkable

from pydantic import JsonValue, TypeAdapter, ValidationError

from herness.core.errors import ConfigError, HernessError, ModelUnavailable, OutputValidationError
from herness.core.logging import get_logger
from herness.core.resilience import aretry_call, call_with_timeout, complete_validated
from herness.core.resilience.metrics import record_counter, timed
from herness.core.types import (
    Answer,
    DecisionInput,
    DecisionOutput,
    LLMRequest,
    LLMResponse,
    Message,
    Question,
    QuestionSet,
    RequestMeta,
    SystemBlock,
    TextPart,
)
from herness.enrich.deciders.openjev import ERRORS_METRIC, LATENCY_METRIC, _asked

__all__ = ["CompletionClient", "LlmDecider", "load_prompt", "vote_distribution", "vote_schema"]

DEFAULT_PROMPT: Final = Path(__file__).resolve().parents[1] / "prompts" / "enrich_decider.md"
_VOTES: Final = frozenset({1, 3, 5})
_ROLE: Final = "enrich_decider"
_SCHEMA_NAME: Final = "enrich_votes"
_SOURCE: Final = "enrich.text_redacted"
_MAX_REPAIRS: Final = 2
_CALL_TIMEOUT_S: Final = 120.0  # per model call; LLMRequest requires one (spec silent)
_MAX_OUTPUT_TOKENS: Final = 2_048  # a vote is ~ 15 tokens per question (≤ 64 questions)
_HEALTH_TIMEOUT_S: Final = 30.0
_HEALTH_PING: Final = "Reply with OK."
# RequestMeta.run_id is a str: the null tracer's run id, as the eval judge uses (report).
_NO_RUN: Final = "run_" + "0" * 26
_BOOL_LABELS: Final = ("true", "false")
_SCORE_LABELS: Final = ("0", "1", "2", "3")
_PARAPHRASES: Final = 5
_HEADINGS: Final = ("System", *(f"Paraphrase {n}" for n in range(1, _PARAPHRASES + 1)))
_HEADING_RE: Final = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
_CLOSE_RE: Final = re.compile(r"</untrusted_data", re.IGNORECASE)
_VERSION: Final[TypeAdapter[str]] = TypeAdapter(
    Annotated[str, DecisionOutput.model_fields["decider_version"]]
)

_log = get_logger("enrich.decider")


@runtime_checkable
class CompletionClient(Protocol):
    """Structural subset of spec 05 `LLMClient` (U03-60, R-05; delta DD-02)."""

    name: str

    def complete(self, req: LLMRequest) -> LLMResponse: ...

    async def acomplete(self, req: LLMRequest) -> LLMResponse: ...


def _labels(question: Question) -> tuple[str, ...]:
    if question.type == "bool":
        return _BOOL_LABELS
    if question.type == "score":
        return _SCORE_LABELS
    return tuple(question.options or {})


def vote_schema(questions: Sequence[Question]) -> dict[str, JsonValue]:
    """JSON Schema of one vote: `{qid: {"answer": enum}}` for every question (U03-61)."""
    props: dict[str, JsonValue] = {
        q.id: {
            "type": "object",
            "properties": {"answer": {"enum": list(_labels(q))}},
            "required": ["answer"],
            "additionalProperties": False,
        }
        for q in questions
    }
    required: list[JsonValue] = [q.id for q in questions]
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def vote_distribution(votes: Sequence[str], labels: Sequence[str]) -> dict[str, float]:
    """`p(a) = (votes(a) + 0.5) / (k + 0.5·K)` for every label, in label order (U03-62).

    Raises OutputValidationError for a vote that is not one of ``labels``.
    """
    counts = dict.fromkeys(labels, 0)
    for vote in votes:
        if vote not in counts:
            msg = "vote outside labels"
            raise OutputValidationError(msg)
        counts[vote] += 1
    denominator = len(votes) + 0.5 * len(counts)
    return {label: (n + 0.5) / denominator for label, n in counts.items()}


def load_prompt(path: Path) -> tuple[str, tuple[str, ...]]:
    """The system header and the five paraphrases of a decider prompt file.

    The format is documented at the top of `prompts/enrich_decider.md`: text before the
    first `## ` heading is never sent; then `## System` and `## Paraphrase 1` ... `5`.
    Raises ConfigError naming only the file when it is missing or malformed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"prompt file {path.name} missing"
        raise ConfigError(msg) from exc
    parts = _HEADING_RE.split(text)[1:]  # [heading, body, heading, body, ...]
    headings, bodies = tuple(parts[0::2]), [body.strip() for body in parts[1::2]]
    if headings != _HEADINGS or not all(bodies):
        msg = f"prompt file {path.name} malformed"
        raise ConfigError(msg)
    return bodies[0], tuple(bodies[1:])


def _wrap(item: DecisionInput) -> str:
    """The R-20 block: `</untrusted_data` (any case) in the text becomes `&lt;/…` first; the
    record id is attribute-escaped so it cannot close the attribute or the tag."""
    body = _CLOSE_RE.sub(lambda m: "&lt;" + m.group(0)[1:], item.text)
    rid = html.escape(item.record_id, quote=True)
    return f'<untrusted_data source="{_SOURCE}" record_id="{rid}">{body}</untrusted_data>'


def _described(question: Question) -> list[str]:
    if question.type == "choice":
        return [f"  - {k}: {v}" for k, v in (question.options or {}).items()]
    if question.type == "score":
        return [f"  - {k}: {v}" for k, v in zip(_SCORE_LABELS, question.levels or (), strict=False)]
    return [f"  - {label}" for label in _BOOL_LABELS]


def _body(asked: Sequence[Question], item: DecisionInput) -> str:
    """The question list and the wrapped text: the user message after the paraphrase."""
    lines = ["Questions:"]
    for q in asked:
        lines.append(f"- id: {q.id}; type: {q.type}; instructions: {q.instructions}")
        lines.extend(_described(q))
    return "\n".join([*lines, "", "Ticket:", _wrap(item)])


def _vote_of(resp: LLMResponse, asked: Sequence[Question]) -> dict[str, str]:
    """The validated reply's label per question (`parsed`, else its decoded text)."""
    obj = resp.parsed if resp.parsed is not None else json.loads(resp.text)
    answers = cast("dict[str, dict[str, str]]", obj)
    return {q.id: answers[q.id]["answer"] for q in asked}


def _answer(votes: Sequence[str], labels: Sequence[str]) -> Answer:
    dist = vote_distribution(votes, labels)
    best = max(labels, key=dist.__getitem__)  # `max` keeps the first of equal values
    return Answer(answer=best, probability=dist[best], distribution=dist, backend_confidence=None)


def _meta(step: int, key: str) -> RequestMeta:
    return RequestMeta(
        run_id=_NO_RUN, task_id=None, role=_ROLE, model_role=_ROLE, step=step, request_key=key
    )


class LlmDecider:
    """k-vote structured-output classifier (U03-63): fallback teacher (D7), escalation
    fallback and deep-mode ensemble member. `samples` (= `votes`) is what the caller
    records with the outputs. One `decide` at a time per instance.
    """

    name = "llm"

    def __init__(
        self,
        client: CompletionClient,
        *,
        version: str,
        votes: int,
        temperature: float,
        max_concurrency: int,
        prompt_path: Path = DEFAULT_PROMPT,
    ) -> None:
        if isinstance(votes, bool) or votes not in _VOTES:
            msg = "votes must be 1, 3 or 5"
            raise ConfigError(msg)
        if max_concurrency < 1:
            msg = "max_concurrency must be >= 1"
            raise ConfigError(msg)
        try:
            self.version = _VERSION.validate_python(version)
        except ValidationError as exc:
            msg = "version is not a decider version"
            raise ConfigError(msg) from exc
        self._client = client
        self._votes = votes
        self._temperature = temperature
        self._max_concurrency = max_concurrency
        self._header, self._paraphrases = load_prompt(prompt_path)

    @property
    def samples(self) -> int:
        """The votes per item: the `samples` value recorded with this decider's outputs."""
        return self._votes

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """`votes` calls per item, ≤ `max_concurrency` items in flight; outputs in input order.

        Raises ModelUnavailable, CircuitOpen, AuthError and EgressBlocked (any error but a
        vote's OutputValidationError); zero valid votes is an item error.
        """
        return asyncio.run(self._adecide(items, questions))

    async def _adecide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        asked = [_asked(item, questions) for item in items]
        gate = asyncio.Semaphore(self._max_concurrency)
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self._one(gate, item, qs))
                    for item, qs in zip(items, asked, strict=True)
                ]
        except BaseExceptionGroup as failed:
            first = failed.exceptions[0]  # the gather is cancelled; the first failure wins
            raise first from first.__cause__
        return [task.result() for task in tasks]

    async def _one(
        self, gate: asyncio.Semaphore, item: DecisionInput, asked: tuple[Question, ...]
    ) -> DecisionOutput:
        if not asked:
            return self._output(item, {}, None)
        body = _body(asked, item)
        async with gate:  # votes of one item run in turn: ≤ max_concurrency calls in flight
            votes = [await self._vote(item, asked, body, i) for i in range(self._votes)]
        valid = [vote for vote in votes if vote is not None]
        if not valid:
            error = OutputValidationError.__name__
            _log.warning("enrich.decide.item_failed", decider=self.name, error_class=error)
            return self._output(item, {}, error)
        answers = {q.id: _answer([v[q.id] for v in valid], _labels(q)) for q in asked}
        return self._output(item, answers, None)

    def _request(
        self, item: DecisionInput, asked: Sequence[Question], body: str, i: int
    ) -> LLMRequest:
        text = f"{self._paraphrases[i % _PARAPHRASES]}\n\n{body}"
        return LLMRequest(
            client=self._client.name,
            system=[SystemBlock(text=self._header)],
            messages=[Message(role="user", parts=[TextPart(text=text)])],
            response_schema=vote_schema(asked),
            response_schema_name=_SCHEMA_NAME,
            max_output_tokens=_MAX_OUTPUT_TOKENS,
            temperature=self._temperature,
            seed=i,
            thinking="off",
            timeout_s=_CALL_TIMEOUT_S,
            metadata=_meta(i, f"{item.content_hash}:{i}"),
        )

    async def _vote(
        self, item: DecisionInput, asked: Sequence[Question], body: str, i: int
    ) -> dict[str, str] | None:
        """One vote, or None when it is still invalid after its repairs (dropped)."""
        req = self._request(item, asked, body, i)
        try:
            with timed(LATENCY_METRIC, component="enrich", labels={"decider": self.name}):
                resp = await aretry_call(
                    "llm_local",
                    complete_validated,
                    self._client,
                    req,
                    max_repairs=_MAX_REPAIRS,
                    breaker_key="decider:llm",
                )
        except HernessError as err:
            labels = {"decider": self.name, "error_class": type(err).__name__}
            record_counter(ERRORS_METRIC, component="enrich", labels=labels)
            if not isinstance(err, OutputValidationError):
                raise
            _log.info(
                "enrich.decide.vote_dropped", decider=self.name, record_id=item.record_id, vote=i
            )
            return None
        return _vote_of(resp, asked)

    def _output(
        self, item: DecisionInput, answers: dict[str, Answer], error: str | None
    ) -> DecisionOutput:
        return DecisionOutput(
            record_id=item.record_id,
            content_hash=item.content_hash,
            decider="llm",
            decider_version=self.version,
            answers=answers,
            error=error,
        )

    def health(self) -> None:
        """A 1-token completion at temperature 0 within 30 s (U03-64).

        Raises ModelUnavailable("llm health") on any failure, a timeout included.
        """
        req = LLMRequest(
            client=self._client.name,
            messages=[Message(role="user", parts=[TextPart(text=_HEALTH_PING)])],
            max_output_tokens=1,
            temperature=0.0,
            thinking="off",
            timeout_s=_HEALTH_TIMEOUT_S,
            metadata=_meta(0, "enrich_decider:health"),
        )
        try:
            call_with_timeout(lambda: self._client.complete(req), _HEALTH_TIMEOUT_S)
        except Exception as exc:
            msg = "llm health"
            raise ModelUnavailable(msg) from exc
