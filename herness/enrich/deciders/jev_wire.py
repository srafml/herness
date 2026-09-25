"""Jev-shape wire mapping, response parsing and adaptive limiter (impl 03 §2, U03-49-U03-51).

Shared by OpenJev, hosted Jev and Laya (design 03 §3.2-§3.3). Decider responses are
untrusted (TH03-06, LLM05): bodies are capped at 1 MB, answers are parsed strictly
against the asked questions, and every violation is an `OutputValidationError` whose
message names the question and the rule, never the offending value.

Open item OI-01 / V-10: whether `probabilities` is an object keyed by label or a list
aligned with option order is not frozen yet, so both shapes are accepted.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from typing import Final, NoReturn, cast

from pydantic import ValidationError

from herness.core import time as clock
from herness.core.errors import ConfigError, OutputValidationError
from herness.core.logging import get_logger
from herness.core.types import Answer, Question

__all__ = [
    "MAX_BODY_BYTES",
    "AdaptiveLimiter",
    "load_wire_body",
    "parse_wire_answers",
    "to_wire_questions",
]

MAX_BODY_BYTES: Final = 1_048_576
_MAX_WIRE_OPTIONS: Final = 255
_SUM_TOL: Final = 1e-3
_SCORE_LABELS: Final = ("0", "1", "2", "3")
_ALLOWED_FIELDS: Final = {
    "bool": frozenset({"noul", "confidence"}),
    "choice": frozenset({"choice", "probabilities", "confidence"}),
    "score": frozenset({"score", "legend", "probabilities", "confidence"}),
}
_REDUCED_FOR_S: Final = 60.0
_MAX_WAIT_S: Final = 1.0

_log = get_logger("enrich.decider")


def _fail(where: str, rule: str) -> NoReturn:
    msg = f"{where}: {rule}"
    raise OutputValidationError(msg)


def _criteria(question: Question) -> dict[str, str] | list[str]:
    if question.type == "score":
        return list(question.levels or ())
    if question.options is None:
        msg = f"{question.id}: choice options are not resolved"
        raise ConfigError(msg)
    if len(question.options) > _MAX_WIRE_OPTIONS:
        msg = f"{question.id}: more than 255 options; shortlist first"
        raise ConfigError(msg)
    return dict(question.options)


def to_wire_questions(questions: Sequence[Question]) -> dict[str, dict[str, object]]:
    """Map questions to the Jev-shape `questions` object (U03-49). Raises ConfigError.

    `bool` -> `noul`; `choice` -> `criteria` {label: description}; `score` -> `criteria`
    [level0..level3]. Key order follows input order.
    """
    wire: dict[str, dict[str, object]] = {}
    for question in questions:
        if question.type == "bool":
            wire[question.id] = {"type": "noul", "instructions": question.instructions}
        else:
            wire[question.id] = {
                "type": question.type,
                "instructions": question.instructions,
                "criteria": _criteria(question),
            }
    return wire


def load_wire_body(body: bytes) -> dict[str, object]:
    """Decode a Jev-shape response body (≤ 1 MB, a JSON object). Raises OutputValidationError.

    Rejects oversized bodies before parsing, and NaN/Infinity constants, invalid UTF-8,
    invalid or too deeply nested JSON and non-object documents (TH03-06).
    """
    if len(body) > MAX_BODY_BYTES:
        _fail("body", "response exceeds 1 MB")
    try:
        data = json.loads(body, parse_constant=_reject_constant)
    except (ValueError, RecursionError):
        _fail("body", "not valid JSON")
    if not isinstance(data, dict):
        _fail("body", "not a JSON object")
    return cast("dict[str, object]", data)


def _reject_constant(_name: str) -> NoReturn:
    msg = "non-finite constant"
    raise ValueError(msg)


def _unit_number(qid: str, value: object, field: str) -> float:
    """Return `value` as a float in [0, 1]; bools, NaN, infinities and big ints fail."""
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value <= 1:
        _fail(qid, f"{field} must be a number in [0, 1]")
    return float(value)


def _aligned_values(
    qid: str, probs: object, labels: Sequence[str], alt: Sequence[str]
) -> list[object]:
    """Probability values in `labels` order from a list or a label- (or `alt`-) keyed object."""
    if isinstance(probs, list):
        if len(probs) != len(labels):
            _fail(qid, "probabilities length does not match the label set")
        return list(probs)
    if not isinstance(probs, Mapping):
        _fail(qid, "probabilities must be an object or a list")
    keys = set(probs)
    if keys == set(labels):
        return [probs[label] for label in labels]
    if len(set(alt)) == len(labels) and keys == set(alt):
        return [probs[key] for key in alt]
    _fail(qid, "probabilities keys do not match the label set")


def _distribution(
    qid: str, probs: object, labels: Sequence[str], alt: Sequence[str] = ()
) -> dict[str, float]:
    values = [_unit_number(qid, v, "probability") for v in _aligned_values(qid, probs, labels, alt)]
    if abs(math.fsum(values) - 1.0) > _SUM_TOL:
        _fail(qid, "probabilities do not sum to 1")
    return dict(zip(labels, values, strict=True))


def _confidence(qid: str, fields: Mapping[str, object]) -> float | None:
    value = fields.get("confidence")
    return None if value is None else _unit_number(qid, value, "confidence")


def _build(
    qid: str, answer: str, distribution: dict[str, float], confidence: float | None
) -> Answer:
    try:
        return Answer(
            answer=answer,
            probability=distribution[answer],
            distribution=distribution,
            backend_confidence=confidence,
        )
    except ValidationError:
        _fail(qid, "answer failed validation")


def _argmax(distribution: dict[str, float]) -> str:
    """Label with the highest probability; ties go to the first in label order."""
    return max(distribution, key=distribution.__getitem__)


def _parse_noul(question: Question, fields: Mapping[str, object]) -> Answer:
    if "noul" not in fields:
        _fail(question.id, "noul is missing")
    p = _unit_number(question.id, fields["noul"], "noul")
    answer = "true" if p >= 0.5 else "false"  # noqa: PLR2004 - the bool decision boundary
    return _build(question.id, answer, {"true": p, "false": 1.0 - p}, None)


def _parse_choice(question: Question, fields: Mapping[str, object]) -> Answer:
    labels = tuple(question.options or ())
    distribution = _distribution(question.id, fields.get("probabilities"), labels)
    answer = _argmax(distribution)
    if "choice" in fields and fields["choice"] != answer:
        _log.debug("enrich.decider.choice_mismatch", decider="jev_wire", question=question.id)
    return _build(question.id, answer, distribution, _confidence(question.id, fields))


def _parse_score(question: Question, fields: Mapping[str, object]) -> Answer:
    probs = fields.get("probabilities")
    distribution = _distribution(question.id, probs, _SCORE_LABELS, question.levels or ())
    # Never the probability-weighted `score` field [J1]; `legend` is ignored.
    return _build(
        question.id, _argmax(distribution), distribution, _confidence(question.id, fields)
    )


def _parse_one(question: Question, value: object) -> Answer:
    if not isinstance(value, Mapping):
        _fail(question.id, "answer must be an object")
    if not set(value) <= _ALLOWED_FIELDS[question.type]:
        _fail(question.id, "answer has unexpected fields")
    fields = cast("Mapping[str, object]", value)
    if question.type == "bool":
        return _parse_noul(question, fields)
    if question.type == "choice":
        return _parse_choice(question, fields)
    return _parse_score(question, fields)


def parse_wire_answers(
    raw: Mapping[str, object], questions: Sequence[Question]
) -> dict[str, Answer]:
    """Validate Jev-shape `answers` into `Answer`s (U03-50). Raises OutputValidationError.

    One `Answer` per asked question present in `raw`, in question order; probabilities
    stay raw. `choice` and `score` answers are the argmax of `probabilities` (ties go to
    the first label); the returned `choice` and weighted `score` fields never win.
    """
    if not isinstance(raw, Mapping):
        _fail("answers", "must be an object")
    asked = {question.id for question in questions}
    if any(key not in asked for key in raw):
        _fail("answers", "unknown question id")
    return {q.id: _parse_one(q, raw[q.id]) for q in questions if q.id in raw}


class AdaptiveLimiter:
    """Async concurrency limit that halves for 60 s after a 429 (U03-51, design 03 §6).

    At most `current_capacity` slots are held at once; new acquisitions also wait until
    the clock reaches the last Retry-After pause. Single event loop only.
    """

    def __init__(self, capacity: int, *, clock: Callable[[], float] = clock.monotonic) -> None:
        if capacity < 1:
            msg = "limiter capacity must be at least 1"
            raise ConfigError(msg)
        self._capacity = capacity
        self._clock = clock
        self._reduced_until = -math.inf
        self._pause_until = -math.inf
        self._in_use = 0
        self._cond = asyncio.Condition()

    @property
    def current_capacity(self) -> int:
        """Half the capacity (at least 1) while reduced after a 429, else the full capacity."""
        if self._clock() < self._reduced_until:
            return max(1, self._capacity // 2)
        return self._capacity

    def on_rate_limited(self, retry_after: float | None) -> None:
        """Halve capacity for 60 s and pause new acquisitions for `retry_after` seconds."""
        now = self._clock()
        self._reduced_until = now + _REDUCED_FOR_S
        self._pause_until = max(self._pause_until, now + (retry_after or 0.0))
        _log.warning("enrich.decider.rate_limited", capacity=self._capacity)

    def _wait_timeout(self) -> float:
        left = self._pause_until - self._clock()
        return min(_MAX_WAIT_S, left) if left > 0 else _MAX_WAIT_S

    def _must_wait(self) -> bool:
        return self._in_use >= self.current_capacity or self._clock() < self._pause_until

    @contextlib.asynccontextmanager
    async def slot(self) -> AsyncIterator[None]:
        """Hold one slot for the body of the `async with`."""
        async with self._cond:
            while self._must_wait():
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._cond.wait(), self._wait_timeout())
            self._in_use += 1
        try:
            yield
        finally:
            async with self._cond:
                self._in_use -= 1
                self._cond.notify_all()
