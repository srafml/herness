"""Security tests for the generated ``decision_wide`` SQL (TH03-19, T03-20)."""

from __future__ import annotations

import pytest

from herness.core.errors import ConfigError
from herness.core.types import Question, QuestionSet
from herness.enrich import resolve

pytestmark = pytest.mark.unit

_QSV = "qs-2026-10-01.1"


def _smuggled(qid: str) -> QuestionSet:
    """A question set whose id bypassed the loader's pattern check (model_construct)."""
    good = Question.model_validate(
        {"id": "q_ok", "type": "bool", "threshold": 0.7, "instructions": "Classify with care."}
    )
    bad = good.model_copy(update={"id": qid})
    return QuestionSet.model_construct(version=_QSV, questions=(good, bad))


@pytest.mark.parametrize(
    "qid",
    [
        'a"; DROP',
        "a'; DROP TABLE enrich.decision; --",
        "q_ok\n",
        "Q_upper",
        "a",
        "q" * 42,
    ],
)
def test_st03_19_injected_question_id_raises_config_error(qid: str) -> None:
    """ST03-19 a question id like ``a"; DROP`` injected past the loader raises ConfigError."""
    with pytest.raises(ConfigError, match="decision_wide_sql") as info:
        resolve.decision_wide_sql(_smuggled(qid))
    assert "DROP" not in str(info.value)  # the rejected id is not echoed


def test_st03_19_valid_ids_quoted() -> None:
    """ST03-19 accepted ids appear only as a quoted identifier and a quoted literal."""
    ddl = resolve.decision_wide_sql(_smuggled("q_fine"))
    assert "question = 'q_fine') AS \"q_fine\"" in ddl
    assert '"q_fine_p"' in ddl
