"""Shared decision types owned by impl 03 (design 03 §3.2; R-01, ENG §14 E6).

The `decisions` submodule of `herness.core.types` (impl 00 owns the package and its
re-export). Imports only `herness.core.errors`. Every model is pydantic v2, frozen,
strict, `extra="forbid"` -- the trust boundary for decider output (TH03-06).
"""

from __future__ import annotations

import math
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, model_validator

from herness.core.errors import ConfigError

QuestionType = Literal["choice", "bool", "score"]
Entity = Literal["incident", "change", "problem"]

_QID_RE = r"^[a-z][a-z0-9_]{1,40}$"
_QS_VERSION_RE = r"^qs-\d{4}-\d{2}-\d{2}(\.\d+)?$"
_OPTION_KEY_RE = re.compile(r"^[A-Za-z0-9_.:-]{1,64}$")
_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{16}$")
_CONTENT_HASH_RE = r"^[0-9a-f]{32}$"
_DECIDER_VERSION_RE = r"^[A-Za-z0-9._:/@+-]{1,128}$"
_ERROR_CLASS_RE = re.compile(r"^[A-Za-z]{1,64}$")
_BOOL_WORDS = frozenset({"true", "false", "yes", "no"})
_MIN_OPTIONS, _MAX_OPTIONS, _MAX_QUESTIONS = 2, 255, 64
_MAX_ANSWERS, _MAX_DISTRIBUTION = 64, 255
_SUM_TOL, _PROB_TOL = 1e-3, 1e-6


def _raise_if(bad: bool, msg: str) -> None:
    if bad:
        raise ValueError(msg)


def _check_description(text: str, field: str) -> None:
    _raise_if(not 1 <= len(text) <= 500, f"bad description length in {field}")  # noqa: PLR2004


class _Frozen(BaseModel):
    """Shared strict, frozen, extra-forbid config for every type in this module."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class Question(_Frozen):
    """One typed classification question (U03-02, design 03 §3.2)."""

    id: str = Field(pattern=_QID_RE)
    type: QuestionType
    instructions: str = Field(min_length=10, max_length=1000)
    options: dict[str, str] | None = None
    options_source: Literal["static", "core.team", "core.service"] = "static"
    levels: tuple[str, str, str, str] | None = None
    applies_to: tuple[Entity, ...] = ("incident",)
    threshold: float = Field(ge=0.5, le=0.999)
    scoring_use: bool = True
    fingerprint: str = ""

    @model_validator(mode="after")
    def _check(self) -> Question:
        self._check_options()
        self._check_levels()
        dupes = len(set(self.applies_to)) != len(self.applies_to)
        _raise_if(not self.applies_to or dupes, "applies_to must be non-empty without duplicates")
        bad_fp = bool(self.fingerprint) and _FINGERPRINT_RE.fullmatch(self.fingerprint) is None
        _raise_if(bad_fp, "bad fingerprint")
        return self

    def _check_options(self) -> None:
        count = 0 if self.options is None else len(self.options)
        if self.type == "choice":
            if self.options_source == "static":
                _raise_if(not _MIN_OPTIONS <= count <= _MAX_OPTIONS, "static options need 2-255")
            else:
                dynamic_too_few = self.options is not None and count < _MIN_OPTIONS
                _raise_if(dynamic_too_few, "dynamic options need at least 2 entries")
        else:
            not_static = self.options is not None or self.options_source != "static"
            _raise_if(not_static, "non-choice question must not have options")
        for key, description in (self.options or {}).items():
            bad_key = _OPTION_KEY_RE.fullmatch(key) is None or key.lower() in _BOOL_WORDS
            _raise_if(bad_key, f"bad option key {key!r}")
            _check_description(description, "options")

    def _check_levels(self) -> None:
        if self.type == "score":
            _raise_if(self.levels is None, "score question needs levels")
            for description in self.levels or ():
                _check_description(description, "levels")
        else:
            _raise_if(self.levels is not None, "non-score question must not have levels")


class QuestionSet(_Frozen):
    """Versioned, ordered set of questions (U03-03)."""

    version: str = Field(pattern=_QS_VERSION_RE)
    questions: tuple[Question, ...]

    _by_id: dict[str, Question] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_unique(self) -> QuestionSet:
        ids = [q.id for q in self.questions]
        _raise_if(len(self.questions) > _MAX_QUESTIONS, "too many questions")
        _raise_if(len(set(ids)) != len(ids), "duplicate question id")
        return self

    def model_post_init(self, context: object, /) -> None:
        self._by_id = {q.id: q for q in self.questions}

    def get(self, qid: str, /) -> Question:
        """Return the question with id ``qid`` (U03-04). Raises ConfigError if unknown."""
        try:
            return self._by_id[qid]
        except KeyError:
            msg = f"unknown question {qid} in {self.version}"
            raise ConfigError(msg) from None

    def for_entity(self, entity: Entity, /) -> QuestionSet:
        """Return the sub-set of questions that apply to ``entity`` (U03-05)."""
        subset = tuple(q for q in self.questions if entity in q.applies_to)
        return QuestionSet(version=self.version, questions=subset)


class DecisionInput(_Frozen):
    """One item to classify (U03-06)."""

    record_id: str = Field(min_length=1, max_length=256)
    entity: Entity
    content_hash: str = Field(pattern=_CONTENT_HASH_RE)
    text: str = Field(min_length=1, max_length=12_000)
    question_ids: tuple[str, ...] | None = None


class Answer(_Frozen):
    """Raw answer of one backend to one question (U03-07). Probabilities are RAW."""

    answer: str
    probability: float = Field(ge=0.0, le=1.0)
    distribution: dict[str, float] = Field(min_length=1, max_length=_MAX_DISTRIBUTION)
    backend_confidence: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Answer:
        total = 0.0
        for value in self.distribution.values():
            out_of_range = not math.isfinite(value) or not 0.0 <= value <= 1.0
            _raise_if(out_of_range, "distribution value out of range")
            total += value
        _raise_if(abs(total - 1.0) > _SUM_TOL, "distribution does not sum to 1")
        _raise_if(self.answer not in self.distribution, "answer not a key of distribution")
        mismatch = abs(self.probability - self.distribution[self.answer]) > _PROB_TOL
        _raise_if(mismatch, "probability does not match distribution[answer]")
        return self


class DecisionOutput(_Frozen):
    """One backend's output for one input (U03-08)."""

    record_id: str
    content_hash: str = Field(pattern=_CONTENT_HASH_RE)
    decider: Literal["laya", "openjev", "jev", "llm", "ensemble", "human"]
    decider_version: str = Field(pattern=_DECIDER_VERSION_RE)
    answers: dict[str, Answer] = Field(max_length=_MAX_ANSWERS)
    error: str | None = None

    @model_validator(mode="after")
    def _check_error(self) -> DecisionOutput:
        if self.error is not None:
            _raise_if(bool(self.answers), "error set with non-empty answers")
            _raise_if(_ERROR_CLASS_RE.fullmatch(self.error) is None, "error must be a class name")
        return self
