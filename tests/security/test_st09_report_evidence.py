"""ST09-31: no raw text from a stored ``result_sample`` reaches report.html, report.md or the
manifest (TH09-11; T09-15d).

The report evidence appendix applies the dashboard's sample policy
(``herness.reports._sample_policy``): ``title``, ``summary``, ``description``, ``notes`` and the
FORBIDDEN_TEXT_COLUMNS are always dropped; every other text column passes ``redact_text`` (the
fixed-key test redactor of ``report_env``); numeric, boolean, date/time and id cells are shown.
"""

from __future__ import annotations

import json
import re
import sqlite3

import duckdb
import pytest
from tests.support.report_env import FUND_RUN, ORG_RUN, ReportEnv

from herness.store.ops import core

pytestmark = pytest.mark.integration

PII = "zq7-planted.pii@example.com"
RAW_TITLE = f"PAY-1 refund for {PII}"
RUN = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
SAMPLE = [
    {"n": 3, "amount": "12.50", "ok": True, "day": "2026-01-02", "run": RUN,
     "title": RAW_TITLE, " Summary ": RAW_TITLE, "description": RAW_TITLE, "NOTES": RAW_TITLE,
     "short_description": RAW_TITLE, "close_notes": RAW_TITLE, "root_cause_text": RAW_TITLE,
     "comment": f"call {PII}", "meta": {"k": "v", "Title": RAW_TITLE}},
]  # fmt: skip


def _plant(env: ReportEnv) -> None:
    """``SAMPLE`` as the result_sample of every ops and warehouse evidence row."""
    stored = json.dumps(SAMPLE)

    def fn(conn: sqlite3.Connection) -> None:
        conn.execute("UPDATE evidence SET result_sample = ?", [stored])

    core.run_write(fn, op="test_seed")
    with duckdb.connect(str(env.warehouse)) as con:
        con.execute("UPDATE meta.evidence SET result_sample = ?", [stored])


@pytest.mark.parametrize("run_id", [FUND_RUN, ORG_RUN], ids=["funding", "org"])
def test_st09_31_raw_sample_text_never_in_report(report_env: ReportEnv, run_id: str) -> None:
    """ST09-31 raw title and planted PII absent from HTML, MD and manifest; text redacted."""
    _plant(report_env)
    report_env.render(run_id)
    html = report_env.out(run_id, "report.html").decode()
    md = report_env.out(run_id, "report.md").decode()
    manifest = report_env.out(run_id, "manifest.json").decode()
    for text in (html, md, manifest):
        assert PII not in text
        assert RAW_TITLE not in text
        assert "PAY-1" not in text
        assert "example.com" not in text
    for text in (html, md):
        # the sample is rendered and its free text redacted (MD escapes ``[``, ``_``, ``]``)
        assert re.search(r"call \\?\[EMAIL\\?_[0-9a-f]+\\?\]", text)
        assert RUN.removeprefix("run_") in text  # ids and typed cells shown (MD escapes _)
        assert "12.50" in text
        assert "2026-01-02" in text
    appendix = html.split('<section id="evidence">', 1)[1].split("</section>", 1)[0]
    assert '<th scope="col">comment</th>' in appendix
    for name in ("title", "Summary", "description", "NOTES", "short_description", "close_notes",
                 "root_cause_text"):  # fmt: skip
        assert f'<th scope="col">{name}</th>' not in appendix
        assert f'<th scope="col"> {name} </th>' not in appendix
