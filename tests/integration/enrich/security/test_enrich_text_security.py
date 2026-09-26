"""ST03-02 (TH03-02) text-stage half: raw core text never reaches ``enrich.text_redacted``.

Carry-over: the spy decider and spy encoder half of ST03-02 (every model input equals a
``text_redacted`` value or a pair text) needs the encode and decide stages and spec 11's
``tiny_build``; neither exists yet. This test covers the text stage on a hand-built
warehouse: known raw emails and raw field values in ``core.*`` never appear in the stored
text, every stored text equals the redaction output, and ``content_hash`` matches it.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest
from tests.support.text_warehouse import Rec, create_warehouse

from herness.core import redact
from herness.core.logging import configure_logging, reset_logging
from herness.enrich import text as tx

pytestmark = pytest.mark.integration

DOMAIN = "corp.test"


@dataclasses.dataclass
class Report:
    """Local stand-in for StageReport (U03-142, not built yet)."""

    rows: int = 0
    cache_hits: int = 0
    failed: int = 0


def _records() -> tuple[list[Rec], list[str]]:
    emails = [f"{name}.{i}@{DOMAIN}" for i, name in enumerate(("ann", "bob", "cy", "di", "ed"))]
    recs = [
        Rec("incident", "INC-1", f"Mail {emails[0]} bounced", f"cc {emails[1]} too"),
        Rec("incident", "INC-2", f"Reset for {emails[2]}", None),
        Rec("change", "CHG-1", f"Owner {emails[3]}", f"approver {emails[4]}"),
        Rec("problem", "PRB-1", None, f"root cause reported by {emails[0]}"),
    ]
    return recs, emails


@pytest.mark.usefixtures("redaction_on")
@pytest.mark.parametrize("incremental", [False, True])
def test_st03_02_no_raw_text_or_email_in_text_redacted(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], incremental: bool
) -> None:
    """ST03-02 stored texts are redaction outputs; no raw value or email is stored or logged."""
    recs, emails = _records()
    prev = None
    if incremental:  # the copy path must carry redacted rows only, too
        prev = tmp_path / "prev.duckdb"
        old = create_warehouse(prev, recs[:2])
        tx.build_text_redacted(old, prev_warehouse=None, report=Report())
        old.close()
    configure_logging("DEBUG")
    try:
        wh = create_warehouse(tmp_path / "new.duckdb", recs)
        tx.build_text_redacted(wh, prev_warehouse=prev, report=Report())
    finally:
        err = capsys.readouterr().err
        reset_logging()
    rows = wh.execute("SELECT record_id, text, content_hash FROM enrich.text_redacted").fetchall()
    assert {row[0] for row in rows} == {rec.record_id for rec in recs}
    raw_values = [v for rec in recs for v in (rec.short, rec.body) if v]
    by_id = {rec.record_id: rec for rec in recs}
    for record_id, text, digest in rows:
        for secret in (*emails, *raw_values):
            assert secret not in text
        assert DOMAIN not in text
        rec = by_id[record_id]
        short = None if rec.entity == "problem" else rec.short
        assert text == redact.redact_text(tx.compose_text(short, rec.body))
        assert digest == tx.content_hash(text)
    assert DOMAIN not in err
    for secret in raw_values:
        assert secret not in err
    events = [json.loads(line)["event"] for line in err.splitlines() if line.strip()]
    assert "enrich.text.redacted" in events
