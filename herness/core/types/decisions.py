"""Shared decision types owned by impl 03 (design 03 §3.2; R-01, ENG §14 E6).

The `decisions` submodule of the `herness.core.types` package; impl 00 owns the
package skeleton and the re-export in `herness/core/types/__init__.py`. This module
imports nothing from `herness` except `herness.core.errors`. All models are pydantic
v2 with ``model_config = ConfigDict(frozen=True, extra="forbid", strict=True)`` and
are the trust boundary for decider output (ENG §3.2, TH03-06).
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
_MIN_STATIC_OPTIONS = 2
_MAX_STATIC_OPTIONS = 255
_MAX_QUESTIONS = 64
_MAX_ANSWERS = 64
_MAX_DISTRIBUTION = 255
_SUM_TOL = 1e-3
_PROB_TOL = 1e-6

_CONFIG = ConfigDict(frozen=True, extra="forbid", strict=True)


def _check_option_key(key: str) -> None:
    if _OPTION_KEY_RE.fullmatch(key) is None or key.lower() in _BOOL_WORDS:
        msg = f"bad option key {key!r}"
        raise ValueError(msg)


def _check_description(text: str, field: str) -> None:
    if not 1 <= len(text) <= 500:  # noqa: PLR2004 - design 03 §3.2 bound
        msg = f"bad description length in {field}"
        raise ValueError(msg)


class Question(BaseModel):
    """One typed classification question (U03-02, design 03 §3.2)."""

    model_config = _CONFIG

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
        if not self.applies_to or len(set(self.applies_to)) != len(self.applies_to):
            msg = "applies_to must be non-empty without duplicates"
            raise ValueError(msg)
        if self.fingerprint and _FINGERPRINT_RE.fullmatch(self.fingerprint) is None:
            msg = "bad fingerprint"
            raise ValueError(msg)
        return self

    def _check_options(self) -> None:
        if self.type == "choice":
            count = 0 if self.options is None else len(self.options)
            if self.options_source == "static":
                if not _MIN_STATIC_OPTIONS <= count <= _MAX_STATIC_OPTIONS:
                    msg = "choice question needs 2-255 static options"
                    raise ValueError(msg)
            elif self.options is not None and count < _MIN_STATIC_OPTIONS:
                msg = "dynamic options need at least 2 entries"
                raise ValueError(msg)
        elif self.options is not None or self.options_source != "static":
            msg = "non-choice question must not have options"
            raise ValueError(msg)
        for key, description in (self.options or {}).items():
            _check_option_key(key)
            _check_description(description, "options")

    def _check_levels(self) -> None:
        if self.type == "score":
            if self.levels is None:
                msg = "score question needs levels"
                raise ValueError(msg)
            for description in self.levels:
                _check_description(description, "levels")
        elif self.levels is not None:
            msg = "non-score question must not have levels"
            raise ValueError(msg)


class QuestionSet(BaseModel):
    """Versioned, ordered set of questions (U03-03)."""

    model_config = _CONFIG

    version: str = Field(pattern=_QS_VERSION_RE)
    questions: tuple[Question, ...]

    _by_id: dict[str, Question] = PrivateAttr(default_factory=dict)

    @model_validator(mode="after")
    def _check_unique(self) -> QuestionSet:
        if len(self.questions) > _MAX_QUESTIONS:
            msg = "too many questions"
            raise ValueError(msg)
        ids = [q.id for q in self.questions]
        if len(set(ids)) != len(ids):
            msg = "duplicate question id"
            raise ValueError(msg)
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


class DecisionInput(BaseModel):
    """One item to classify (U03-06)."""

    model_config = _CONFIG

    record_id: str = Field(min_length=1, max_length=256)
    entity: Entity
    content_hash: str = Field(pattern=_CONTENT_HASH_RE)
    text: str = Field(min_length=1, max_length=12_000)
    question_ids: tuple[str, ...] | None = None


class Answer(BaseModel):
    """Raw answer of one backend to one question (U03-07). Probabilities are RAW."""

    model_config = _CONFIG

    answer: str
    probability: float = Field(ge=0.0, le=1.0)
    distribution: dict[str, float] = Field(min_length=1, max_length=_MAX_DISTRIBUTION)
    backend_confidence: float | None = None

    @model_validator(mode="after")
    def _check(self) -> Answer:
        total = 0.0
        for value in self.distribution.values():
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                msg = "distribution value out of range"
                raise ValueError(msg)
            total += value
        if abs(total - 1.0) > _SUM_TOL:
            msg = "distribution does not sum to 1"
            raise ValueError(msg)
        if self.answer not in self.distribution:
            msg = "answer not a key of distribution"
            raise ValueError(msg)
        if abs(self.probability - self.distribution[self.answer]) > _PROB_TOL:
            msg = "probability does not match distribution[answer]"
            raise ValueError(msg)
        return self


class DecisionOutput(BaseModel):
    """One backend's output for one input (U03-08)."""

    model_config = _CONFIG

    record_id: str
    content_hash: str = Field(pattern=_CONTENT_HASH_RE)
    decider: Literal["laya", "openjev", "jev", "llm", "ensemble", "human"]
    decider_version: str = Field(pattern=_DECIDER_VERSION_RE)
    answers: dict[str, Answer] = Field(default_factory=dict, max_length=_MAX_ANSWERS)
    error: str | None = None

    @model_validator(mode="after")
    def _check_error(self) -> DecisionOutput:
        if self.error is not None:
            if self.answers:
                msg = "error set with non-empty answers"
                raise ValueError(msg)
            if _ERROR_CLASS_RE.fullmatch(self.error) is None:
                msg = "error must be a bare error class name"
                raise ValueError(msg)
        return self
