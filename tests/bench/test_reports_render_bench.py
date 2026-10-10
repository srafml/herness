"""Renderer benchmarks BT09-05 and BT09-06 (impl 09 §10; T09-11).

Run: `pytest -m "integration and slow" tests/bench/test_reports_render_bench.py`. Dataset:
the T11-17 `tiny_build` copy of `tests.support.report_env` (the `full` build is too heavy for
the dev box, sub-controller ruling) with the funding fixture draft scaled to 300 evidence
entries: the fixture's five plus 295 extra ops `evidence` rows, each with a 20-row sample
(`evidence_sample_rows`), cited by one number each across eight executive-summary paragraphs.
BT09-05 times `render_run` (HTML + MD) over `REPEATS` renders and gates on the median; the PDF
part runs only when WeasyPrint is installed (optional `pdf` extra), else it is skipped.
BT09-06 records the size of `report.html`. Each writes one stderr line.
"""

from __future__ import annotations

import sys
import time
from typing import Any, Final

import pytest
from tests.support.bench import BenchRecorder, BenchTarget, record_median, record_value
from tests.support.bench_stats import REPEATS
from tests.support.report_env import (
    F1,
    FUND_RUN,
    Evidence,
    ReportEnv,
    fixture_draft,
    insert_evidence,
    number,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

EXTRA: Final = 295
PER_PARAGRAPH: Final = 40
RENDER: Final = BenchTarget("BT09-05", "09", "render_seconds", "s", 30.0, "tiny")
PDF: Final = BenchTarget("BT09-05", "09", "pdf_extra_seconds", "s", 30.0, "tiny")
SIZE: Final = BenchTarget("BT09-06", "09", "report_html_mb", "MB", 5.0, "tiny")


def _scaled(env: ReportEnv) -> int:
    """Insert the extra evidence, write the scaled draft; the number of evidence entries."""
    extra = [
        Evidence("SELECT n FROM metrics.incident_fact WHERE bucket = $b", {"b": i},
                 [{"bucket": str(i), "row": str(j), "n": str(i * j)} for j in range(20)])
        for i in range(EXTRA)
    ]  # fmt: skip
    insert_evidence(extra)
    draft = fixture_draft("funding")
    paragraphs: list[dict[str, Any]] = []
    for start in range(0, EXTRA, PER_PARAGRAPH):
        chunk = extra[start : start + PER_PARAGRAPH]
        text = " ".join(f"Bucket value [[n{k + 1}]]." for k in range(len(chunk)))
        numbers = [number(f"n{k + 1}", e.qid) for k, e in enumerate(chunk)]
        paragraphs.append({"text": text, "numbers": numbers, "finding_ids": [F1]})
    draft["sections"][0]["paragraphs"].extend(paragraphs)
    env.write_draft(draft)
    return 5 + EXTRA


def test_bt09_05_render_300_evidence(report_env: ReportEnv,
                                     bench_recorder: BenchRecorder) -> None:  # fmt: skip
    """BT09-05 ``render_run`` HTML + MD with 300 evidence entries < 30 s (median)."""
    expected = _scaled(report_env)
    report_env.render(FUND_RUN)  # warm-up
    times: list[float] = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        manifest = report_env.render(FUND_RUN)
        times.append(time.perf_counter() - start)
    assert manifest.evidence_entries == expected
    assert manifest.numbers_linked == manifest.numbers_total
    rec = record_median(bench_recorder, RENDER, times)
    assert rec.passed, rec
    size_mb = len(report_env.out(FUND_RUN, "report.html")) / (1024 * 1024)
    sys.stderr.write(f"BT09-06 report_html_mb {size_mb:.4f} MB (target < 5)\n")
    size = record_value(bench_recorder, SIZE, size_mb)
    assert size.passed, size


def test_bt09_05_pdf_extra(report_env: ReportEnv, bench_recorder: BenchRecorder) -> None:
    """BT09-05 the PDF adds < 30 s (median); needs WeasyPrint (optional ``pdf`` extra)."""
    pytest.importorskip("weasyprint")
    _scaled(report_env)
    base: list[float] = []
    with_pdf: list[float] = []
    for _ in range(REPEATS):
        start = time.perf_counter()
        report_env.render(FUND_RUN)
        base.append(time.perf_counter() - start)
        start = time.perf_counter()
        report_env.render(FUND_RUN, ["html", "md", "pdf"])
        with_pdf.append(time.perf_counter() - start)
    extra = [max(0.0, p - b) for p, b in zip(with_pdf, base, strict=True)]
    rec = record_median(bench_recorder, PDF, extra)
    assert rec.passed, rec
