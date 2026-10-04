"""Job payload builders of the long-running CLI commands (impl 09 U09-92).

Pure functions: every payload is JSON-serialisable and holds no secret or record text. The
sync, resume, eval and maintenance payloads belong to their owning specs (R-07, R-09).
Payload keys follow the U09-92 table (DD-10 default).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from decimal import Decimal
from typing import Final, Literal

from pydantic import ValidationError

from herness.harness.swarm import RunRequest
from herness.metrics.portfolio import Scenario
from herness.reports.rules import UserInputError

__all__ = ["STAGE_ORDER", "parse_budget_usd", "pipeline_payload", "review_request", "stages_from"]

STAGE_ORDER: Final = ("build", "enrich", "score", "dq", "promote")

_MAX_BUDGET_CHARS: Final = 32
_BUDGET_RE: Final = re.compile(r"[0-9]+(?:[_,][0-9]+)*(?:\.00)?", re.ASCII)
_MIN_BUDGET: Final = Decimal(1)
_MAX_BUDGET: Final = Decimal(10**12)
_BUDGET_MSG: Final = "Budget must be a whole number of US dollars from 1 to 1,000,000,000,000."
_BUDGET_FIX: Final = "Use digits with optional `,` or `_` separators, for example 2,000,000."


def _check_stage(stage: str) -> None:
    if stage not in STAGE_ORDER:
        msg = "unknown stage name"
        raise UserInputError(msg, hint=f"Use one of: {', '.join(STAGE_ORDER)}.")


def stages_from(stage: str) -> list[str]:
    """``--from-stage S``: the suffix of ``STAGE_ORDER`` starting at ``S`` (U09-92)."""
    _check_stage(stage)
    return list(STAGE_ORDER[STAGE_ORDER.index(stage) :])


def pipeline_payload(
    stages: Sequence[str], build_id: str | None, **extra: object
) -> dict[str, object]:
    """``{"stages", "build_id", **extra}``; ``extra`` holds ``enrich_stage``, ``depth`` or
    ``score_steps`` (U09-92). An unknown stage name raises UserInputError."""
    for stage in stages:
        _check_stage(stage)
    return {"stages": list(stages), "build_id": build_id, **extra}


def review_request(
    kind: Literal["funding_review", "org_review"],
    depth: str,
    budgets_usd: Sequence[Decimal],
    question: str | None = None,
) -> dict[str, object]:
    """``{"request": RunRequest(...)}`` in JSON form, one ``custom_<int>`` scenario per budget.

    An invalid request (unknown depth, more than five budgets) raises UserInputError.
    """
    try:
        scenarios = [Scenario(name=f"custom_{int(b)}", budget_usd=b) for b in budgets_usd]
        fields = {"kind": kind, "depth": depth, "scenarios": scenarios, "question": question}
        request = RunRequest.model_validate(fields)
    except ValidationError:
        msg = "invalid review request"
        hint = "Use a known depth and at most five budgets."
        raise UserInputError(msg, hint=hint) from None
    return {"request": request.model_dump(mode="json")}


def parse_budget_usd(text: str) -> Decimal:
    """A whole-dollar budget: digits with optional ``_``/``,`` separators and an optional
    ``.00``; 1 ≤ value ≤ 10^12, else UserInputError (U09-92). The input is never echoed."""
    if len(text) > _MAX_BUDGET_CHARS or _BUDGET_RE.fullmatch(text) is None:
        raise UserInputError(_BUDGET_MSG, hint=_BUDGET_FIX)
    value = Decimal(text.removesuffix(".00").replace("_", "").replace(",", ""))
    if not _MIN_BUDGET <= value <= _MAX_BUDGET:
        raise UserInputError(_BUDGET_MSG, hint=_BUDGET_FIX)
    return value
