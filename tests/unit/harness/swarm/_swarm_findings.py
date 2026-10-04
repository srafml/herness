"""Finding, run-row and knob factories for the T06-16 dedup and adversarial tests."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from herness.core.types import Depth, Finding, NumberRef
from herness.harness.pipelines.settings import DepthKnobs, PipelinesConfig, resolve_knobs
from herness.store.ops.runs import RunRow

RUN_ID = "run_" + "0" * 26
T0 = datetime(2026, 9, 26, 10, 0, tzinfo=UTC)


def fid(i: int) -> str:
    """A valid finding id that sorts by `i`."""
    return f"fnd_{i:026d}"


def tid(i: int) -> str:
    """A valid task id that sorts by `i`."""
    return f"task_{i:026d}"


def qid(i: int) -> str:
    """A valid query id."""
    return f"q_{i:016x}"


def number(
    n: int,
    *,
    q: int = 1,
    unit: str = "count",
    column: str = "incidents",
    row_key: dict[str, Any] | None = None,
) -> NumberRef:
    value: object = "1200.50" if unit == "usd" else 7
    return NumberRef.model_validate(
        {
            "id": f"n{n}",
            "value": value,
            "unit": unit,
            "query_id": qid(q),
            "column": column,
            "row_key": row_key,
        }
    )


def finding(  # noqa: PLR0913 - test factory with keyword defaults
    i: int,
    *,
    entity: tuple[str, str] = ("team", "t1"),
    numbers: list[NumberRef] | None = None,
    confidence: float = 0.5,
    task: int = 1,
    claim: str = "Team t1 had [[n1]] incidents",
    created: int = 0,
) -> Finding:
    nums = numbers if numbers is not None else [number(1)]
    return Finding.model_validate(
        {
            "finding_id": fid(i),
            "run_id": RUN_ID,
            "task_id": tid(task),
            "author_role": "analyst",
            "claim": claim,
            "entity_type": entity[0],
            "entity_id": entity[1],
            "numbers": nums,
            "query_ids": sorted({n.query_id for n in nums}),
            "confidence": confidence,
            "created_at": T0 + timedelta(seconds=created),
        }
    )


def knobs(depth: Depth, **override: int) -> DepthKnobs:
    base = resolve_knobs(PipelinesConfig(), "funding_review", depth)
    return base.model_copy(update=override) if override else base


def run_row(depth: str = "standard", profile: str = "local") -> RunRow:
    return RunRow(
        run_id=RUN_ID,
        kind="funding_review",
        depth=depth,
        profile=profile,
        build_id="20260925-101500-ABCDEF",
        status="running",
        started_at=T0,
        finished_at=None,
        token_usage={},
        cost_usd=Decimal(0),
        config_hash="h",
        meta={},
    )
