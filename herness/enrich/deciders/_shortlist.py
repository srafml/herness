"""Per-record option shortlist for the LLM and Jev deciders (impl 03 U03-19, TH03-09).

A choice question with more than 255 options (a dynamic source such as `owning_team`) is
replaced, per record, by its 64 options closest to the record text before any schema or wire
body is built. Option vectors are embedded once per question per `decide` call from
`"<label>: <description>"`. Fails closed: no `embed_fn`, or an embedding error, raises before
any model or HTTP call.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Final

import numpy as np

from herness.core.errors import ConfigError
from herness.core.types import DecisionInput, Question, QuestionSet
from herness.enrich.questions import PAIR_QUESTIONS, shortlist_options

__all__ = ["EmbedFn", "asked_for", "shortlist_asked"]

EmbedFn = Callable[[Sequence[str]], np.ndarray]
_WIDE: Final = 255  # the wire limit (jev_wire): above it a question must be shortlisted
_K: Final = 64


def asked_for(item: DecisionInput, questions: QuestionSet) -> tuple[Question, ...]:
    """Questions asked for `item` (impl 03 §3.9 shared rules)."""
    if item.question_ids is not None:
        return tuple(questions.get(qid) for qid in item.question_ids)
    subset = questions.for_entity(item.entity).questions
    return tuple(q for q in subset if q.id not in PAIR_QUESTIONS)


def shortlist_asked(
    items: Sequence[DecisionInput], questions: QuestionSet, embed_fn: EmbedFn | None
) -> list[tuple[Question, ...]]:
    """The questions asked per item, each choice question above 255 options cut to the top 64
    options for that item's text."""
    asked = [asked_for(item, questions) for item in items]
    wide = {
        q.id: q for qs in asked for q in qs if q.type == "choice" and len(q.options or {}) > _WIDE
    }
    if not wide:
        return asked
    if embed_fn is None:
        msg = "a question with more than 255 options needs an embed_fn to shortlist"
        raise ConfigError(msg, question=next(iter(wide)))
    option_vecs: dict[str, dict[str, np.ndarray]] = {}
    for qid, q in wide.items():
        options = q.options or {}
        labels = sorted(options)
        vecs = embed_fn([f"{label}: {options[label]}" for label in labels])
        option_vecs[qid] = dict(zip(labels, vecs, strict=True))
    out: list[tuple[Question, ...]] = []
    for item, qs in zip(items, asked, strict=True):
        if not any(q.id in wide for q in qs):
            out.append(qs)
            continue
        text_vec = embed_fn([item.text])[0]
        out.append(
            tuple(
                shortlist_options(q, text_vec, option_vecs[q.id], k=_K) if q.id in wide else q
                for q in qs
            )
        )
    return out
