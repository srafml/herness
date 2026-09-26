"""IT03-02 incremental text stage over two consecutive builds (impl 03 F03-02, T03-05).

Uses the real ``redact_table`` (spec 10) with a keyring-backed test config; chunks stay
below the pool's chunk size, so redaction runs in-process.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pyarrow as pa
import pytest
from tests.support.text_warehouse import Rec, create_warehouse, stored

from herness.core import redact
from herness.enrich import text as tx

pytestmark = pytest.mark.integration

DOMAIN = "corp.test"


@dataclasses.dataclass
class Report:
    """Local stand-in for StageReport (U03-142, not built yet)."""

    rows: int = 0
    cache_hits: int = 0
    failed: int = 0


def _records() -> list[Rec]:
    recs: list[Rec] = []
    for i in range(300):
        entity = ("incident", "change", "problem")[i % 3]
        mail = f"user{i}@{DOMAIN}"
        short = None if entity == "problem" else f"Ticket {i} from {mail}"
        recs.append(Rec(entity, f"{entity[:3].upper()}-{i:04d}", short, f"details {i}; ask {mail}"))
    return recs


def _redacted(rec: Rec) -> str:
    short = None if rec.entity == "problem" else rec.short
    out = redact.redact_text(tx.compose_text(short, rec.body))
    assert out is not None
    return out


@pytest.mark.usefixtures("redaction_on")
def test_it03_02_second_build_copies_unchanged_and_redacts_changed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT03-02 two builds, 1 % changed: unchanged rows copied, changed rows re-redacted."""
    first = _records()
    wh1 = create_warehouse(tmp_path / "b1.duckdb", first)
    report1 = Report()
    tx.build_text_redacted(wh1, prev_warehouse=None, report=report1)
    assert (report1.rows, report1.cache_hits, report1.failed) == (300, 0, 0)
    before = stored(wh1)
    wh1.close()

    changed_at = {7, 150, 299}  # 1 % of 300: one per entity
    second = [
        rec.touched(body=f"edited {i}; mail new{i}@{DOMAIN}") if i in changed_at else rec
        for i, rec in enumerate(first)
    ]
    real = redact.redact_table
    seen: list[str] = []

    def spy(tbl: pa.Table, text_cols: list[str], id_col: str = "record_id") -> pa.Table:
        seen.extend(tbl.column(id_col).to_pylist())
        return real(tbl, text_cols, id_col)

    monkeypatch.setattr(redact, "redact_table", spy)
    wh2 = create_warehouse(tmp_path / "b2.duckdb", second)
    report2 = Report()
    tx.build_text_redacted(wh2, prev_warehouse=tmp_path / "b1.duckdb", report=report2)

    assert (report2.rows, report2.cache_hits, report2.failed) == (300, 297, 0)
    changed_ids = {second[i].record_id for i in changed_at}
    assert set(seen) == changed_ids
    assert len(seen) == len(changed_ids)
    after = stored(wh2)
    assert set(after) == set(before)
    for rec in second:
        if rec.record_id in changed_ids:
            entity, text, digest = after[rec.record_id]
            assert (entity, text) == (rec.entity, _redacted(rec))
            assert text != before[rec.record_id][1]
            assert digest == tx.content_hash(text)
        else:
            assert after[rec.record_id] == before[rec.record_id]
