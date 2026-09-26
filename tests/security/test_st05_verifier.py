"""Security tests for herness.harness.verifier.Verifier (ST05-11, ST05-12, ST05-23).

Threats TH05-11 (fabricated or wrong numbers), TH05-12 (edited or invented evidence) and
TH05-23 (Unicode digits slipping past the numeral scan). Runs on the test-local stand-in
build of `_verifier_standin` (spec 11 `tiny_build` does not exist yet). ST05-12 uses the real
ops store `evidence` area so the `query_id` recompute on read (U05-71) is exercised.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.harness_fakes import FakeOps
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness import _verifier_standin as sd

from herness.core.types import OpsHandle
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.verifier import Verifier
from herness.harness.warehouse import WarehousePool
from herness.store.ops import core
from herness.store.ops import evidence as ops_evidence

pytestmark = pytest.mark.unit

PAY = {"team": "payments", "week": "2026-09-21"}


@pytest.fixture
def pool(tmp_path: Path) -> Iterator[WarehousePool]:
    sd.make_build(tmp_path / "wh")
    handle = WarehousePool(tmp_path / "wh", SqlSettings())
    try:
        yield handle
    finally:
        handle.close_all()


def _verifier(ops: OpsHandle, pool: WarehousePool) -> Verifier:
    return Verifier(
        ops, pool, VerifierSettings(), allowed_numeral_patterns=sd.PATTERNS, sql=SqlSettings()
    )


def test_st05_11_fabricated_and_wrong_numbers_rejected(pool: WarehousePool) -> None:
    """ST05-11 fabricated number, wrong column, wrong row, off-by-one-cent USD: all rejected."""
    ops = FakeOps()
    team = sd.record(pool, sd.TEAM_SQL)
    ops.record_evidence(team)
    verifier = _verifier(ops, pool)
    attacks = {
        "fabricated": sd.ref("n1", 120, team.query_id, "incidents", PAY),
        "wrong_column": sd.ref("n1", 12, team.query_id, "mttr_hours", PAY, unit="hours"),
        "wrong_row": sd.ref("n1", 12, team.query_id, "incidents", {"team": "search"}),
        "usd_cent": sd.ref("n1", "1234.49", team.query_id, "cost_usd", PAY, unit="usd"),
    }
    outcomes = {
        name: verifier.verify_numbers(sd.item("Claim [[n1]].", [number]), sd.BUILD_ID)
        for name, number in attacks.items()
    }
    assert {name: r.passed for name, r in outcomes.items()} == dict.fromkeys(attacks, False)
    assert {name: r.items[0].checks[0].result for name, r in outcomes.items()} == {
        "fabricated": "mismatch",
        "wrong_column": "mismatch",
        "wrong_row": "mismatch",
        "usd_cent": "mismatch",
    }
    honest = sd.ref("n1", "1234.50", team.query_id, "cost_usd", PAY, unit="usd")
    assert verifier.verify_numbers(sd.item("Cost [[n1]].", [honest]), sd.BUILD_ID).passed


def test_st05_12_edited_invented_and_foreign_evidence(
    ops_store: OpsStoreHandle, pool: WarehousePool
) -> None:
    """ST05-12 edited evidence.sql, fabricated query_id, another build's query_id:
    missing_query, missing_query, wrong_build."""
    edited = sd.record(pool, sd.TEAM_SQL)
    honest = sd.record(pool, sd.ONE_ROW_SQL)
    foreign = sd.foreign_evidence(sd.OTHER_BUILD_SQL, build_id=sd.OTHER_BUILD_ID)
    for ev in (edited, honest, foreign):
        assert ops_evidence.record_evidence(ev)
    fake_sql = "SELECT team, week, 13 AS incidents FROM metrics.team_week"
    core.run_write(
        lambda conn: conn.execute(
            "UPDATE evidence SET sql = ? WHERE query_id = ?", (fake_sql, edited.query_id)
        ),
        op="test_edit_evidence_sql",
    )
    verifier = _verifier(ops_evidence, pool)
    numbers = [
        sd.ref("n1", 13, edited.query_id, "incidents", PAY),
        sd.ref("n2", 12, "q_" + "f" * 16, "incidents", PAY),
        sd.ref("n3", 1, foreign.query_id, "x", None),
        sd.ref("n4", 5, honest.query_id, "n", None),
    ]
    result = verifier.verify_numbers(sd.item("[[n1]] [[n2]] [[n3]] [[n4]]", numbers), sd.BUILD_ID)
    assert [c.result for c in result.items[0].checks] == [
        "missing_query",
        "missing_query",
        "wrong_build",
        "match",
    ]
    assert not result.passed


@pytest.mark.parametrize(
    "text",
    [
        "Payments had １２ incidents.",  # noqa: RUF001 - deliberate full-width digits (TH05-23)
        "Costs rose x² this quarter.",
        "About ½ of the teams.",
        "Arabic-Indic ١٢ incidents.",
        "Extended Arabic-Indic ۱۲ incidents.",
    ],
)
def test_st05_23_unicode_numerals_flagged_uncited(pool: WarehousePool, text: str) -> None:
    """ST05-23 full-width digits, superscripts, fractions and Arabic-Indic digits: uncited."""
    verifier = _verifier(FakeOps(), pool)
    result = verifier.verify_numbers(sd.item(text, []), sd.BUILD_ID)
    got = result.items[0]
    assert got.uncited
    assert all(text[u.start : u.end] == u.text for u in got.uncited)
    assert not got.passed
    assert not result.passed
