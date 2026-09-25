"""Shared types of design 00 §6, re-exported from their owner submodules (impl 00 §3.5).

This file defines nothing. Each owner card (T03-01, T05-01, T06-01, T07-01, T08-01,
T09-01) adds one import line for its submodule and its names to __all__.
"""

from herness.core.types.decisions import (
    Answer,
    DecisionInput,
    DecisionOutput,
    Entity,
    Question,
    QuestionSet,
    QuestionType,
)

__all__: tuple[str, ...] = (
    "Answer",
    "DecisionInput",
    "DecisionOutput",
    "Entity",
    "Question",
    "QuestionSet",
    "QuestionType",
)
