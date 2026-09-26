"""Verifier benchmark (BT05-09). Run: pytest -m "integration and slow" tests/bench.

Stand-in for "100 findings from scripted runs on `full`" (spec 11's `full` build and the
scripted runs are not built yet): the test-local `_verifier_standin` build grown by 200,000
bulk rows, and 100 findings of up to 5 numbers over up to 3 queries. Each finding is timed on a
fresh `Verifier` (cold re-run cache, the worst case).
"""

from __future__ import annotations

import statistics
import time
from pathlib import Path

import pytest
from tests.support.harness_fakes import FakeOps
from tests.unit.harness import _verifier_standin as sd

from herness.core.types import ChatAnswer, Finding, NumberRef
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool

pytestmark = [pytest.mark.integration, pytest.mark.slow]

FINDING_P95_S = 3.0
ANSWER_P95_S = 5.0
BULK_ROWS = 200_000


PAY_WEEKS_SQL = (
    "SELECT week, sum(incidents) AS incidents FROM metrics.team_week"
    " WHERE team = $team GROUP BY week"
)


WEEK_ROWS = [row for row in sd.TEAM_ROWS if row[1].isoformat() == sd.WEEK_BIND["week"]]


def _numbers(qids: dict[str, str], i: int) -> list[NumberRef]:
    """3 to 5 numbers over 2 or 3 queries, cycling through the teams of the bound week."""
    team, _week, incidents, _mttr, cost, _ticket = WEEK_ROWS[i % len(WEEK_ROWS)]
    numbers = [
        sd.ref("n1", incidents, qids["by_week"], "incidents", {"team": team}),
        sd.ref("n2", str(cost), qids["by_week"], "cost_usd", {"team": team}, unit="usd"),
        sd.ref("n3", len(sd.TEAM_ROWS) + BULK_ROWS, qids["one_row"], "n", None),
        sd.ref("n4", 12, qids["pay_weeks"], "incidents", {"week": "2026-09-21"}),
        sd.ref("n5", 7, qids["pay_weeks"], "incidents", {"week": "2026-09-14"}),
    ]
    return numbers[: 3 + i % 3]


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=100)[94]


def test_bt05_09_verifier_per_finding_and_answer_p95(tmp_path: Path) -> None:
    """BT05-09 p95 per finding (<= 5 numbers, <= 3 queries) < 3 s; verify_answer p95 <= 5 s."""
    sd.make_build(tmp_path, extra_rows=BULK_ROWS)
    pool = WarehousePool(tmp_path, SqlSettings())
    ops = FakeOps()
    try:
        qids = {}
        recorded = sd.recorded_queries(pool)
        recorded["pay_weeks"] = sd.record(pool, PAY_WEEKS_SQL, {"team": "payments"})
        for name, ev in recorded.items():
            ops.record_evidence(ev)
            qids[name] = ev.query_id
        finding_s: list[float] = []
        answer_s: list[float] = []
        for i in range(100):
            numbers = _numbers(qids, i)
            text = " ".join(f"[[{n.id}]]" for n in numbers) + " in Q3 2026."
            finding = Finding(
                finding_id=f"fnd_{i:026d}",
                run_id="run_" + "C" * 26,
                task_id="task_" + "D" * 26,
                author_role="analyst",
                claim=text,
                entity_type="team",
                entity_id="payments",
                numbers=numbers,
                query_ids=sorted({n.query_id for n in numbers}),
                confidence=0.8,
                created_at=sd.EXECUTED_AT,
            )
            verifier = Verifier(
                ops,
                pool,
                VerifierSettings(),
                allowed_numeral_patterns=sd.PATTERNS,
                sql=SqlSettings(),
            )
            t0 = time.perf_counter()
            [result] = verifier.verify_findings([finding], sd.BUILD_ID)
            finding_s.append(time.perf_counter() - t0)
            assert result.passed, result.items[0]
            answer = ChatAnswer(text=text, numbers=numbers, query_ids=finding.query_ids)
            fresh = Verifier(
                ops,
                pool,
                VerifierSettings(),
                allowed_numeral_patterns=sd.PATTERNS,
                sql=SqlSettings(),
            )
            t0 = time.perf_counter()
            assert fresh.verify_answer(answer, sd.BUILD_ID).passed
            answer_s.append(time.perf_counter() - t0)
    finally:
        pool.close_all()
    assert _p95(finding_s) < FINDING_P95_S, f"finding p95 {_p95(finding_s):.3f} s"
    assert _p95(answer_s) <= ANSWER_P95_S, f"answer p95 {_p95(answer_s):.3f} s"
