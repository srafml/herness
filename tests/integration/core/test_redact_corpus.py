"""IT10-02 redaction quality over the spec 11 labelled corpus (impl 10 T10-15, R-56).

Corpus: `write_pii_corpus(11, 5000, <tmp>)`; the redactor's name directory holds the corpus's
invented names. Matching rules, applied to `Redactor.scan` output against the labels:

* a labelled span is *recalled* when a scanned span of the same type overlaps it (a partial
  overlap counts: the value is still masked); the exact-boundary rate is reported separately.
  `CREDENTIAL` and `URL_TOKEN` form one class (`SECRET_CLASS`): both are replaced by
  `[SECRET]`, and the corpus's `synthetic<hex>` token values inside a `?token=` URL are claimed
  by the earlier `CREDENTIAL` detector (DETECTION_ORDER), so the value is masked either way. The
  strict same-type rate for URL_TOKEN is 0.0 and is reported on the measurement line;
* PERSON: the directory lists each corpus name as a display name (`First Last`,
  `Last, First`) plus the `F. Last` form in `alt_names` (the corpus's third PERSON format; the
  designed directory has no initials variant, so without it recall is about 0.67);
* a scanned span is a *true positive* when it overlaps any labelled span (any type), else a
  false positive; precision = true positives / scanned spans;
* "keep" ticket ranges (INC/CHG/PRB numbers) must not overlap any scanned span.

Thresholds (spec 10 IT10-02): recall >= 0.99 per patterned type, PERSON >= 0.95, precision
>= 0.90, zero masked ticket numbers.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Any

import pytest
from tests.support.redact_quality import build_directory, load_corpus, make_redactor, write_corpus

pytestmark = pytest.mark.integration

SECRET_CLASS = frozenset({"CREDENTIAL", "URL_TOKEN"})
PATTERNED = ("EMAIL", "PHONE", "IP", "EMPLOYEE_ID", "CARD", "CREDENTIAL", "URL_TOKEN")


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("pii_corpus")
    records = load_corpus(write_corpus(work))
    red = make_redactor(build_directory(work, initials=True))
    total: Counter[str] = Counter()
    hit: Counter[str] = Counter()
    exact: Counter[str] = Counter()
    strict: Counter[str] = Counter()
    true_pos = scanned = 0
    masked_tickets: list[str] = []
    for rec in records:
        found = red.scan(rec["text"])
        scanned += len(found)
        for gold in rec["spans"]:
            total[gold["type"]] += 1
            kind = gold["type"]
            over = [s for s in found if s.start < gold["end"] and gold["start"] < s.end]
            same = [s for s in over if s.type == kind]
            hit[kind] += any(s.type == kind or {s.type, kind} <= SECRET_CLASS for s in over)
            strict[kind] += bool(same)
            exact[kind] += any(s.start == gold["start"] and s.end == gold["end"] for s in same)
        for s in found:
            true_pos += any(g["start"] < s.end and s.start < g["end"] for g in rec["spans"])
        masked_tickets += [
            f"{rec['id']}:{rec['text'][k['start'] : k['end']]}"
            for k in rec["keep"]
            if any(s.start < k["end"] and k["start"] < s.end for s in found)
        ]
    recall = {t: hit[t] / total[t] for t in total}
    precision = true_pos / scanned
    per_type = " ".join(
        f"{t}={recall[t]:.4f}(n={total[t]},strict={strict[t] / total[t]:.4f},"
        f"exact={exact[t] / total[t]:.4f})"
        for t in sorted(total)
    )
    sys.stderr.write(
        f"IT10-02 recall {per_type} precision={precision:.4f} (tp={true_pos}, scanned={scanned})"
        f" masked_tickets={len(masked_tickets)}\n"
    )
    return {"recall": recall, "total": total, "precision": precision, "tickets": masked_tickets}


def test_it10_02_corpus_has_every_type(measured: dict[str, Any]) -> None:
    """The corpus exercises every patterned type and PERSON (guards against vacuous recall)."""
    for kind in (*PATTERNED, "PERSON"):
        assert measured["total"][kind] >= 100, kind


@pytest.mark.parametrize("kind", PATTERNED)
def test_it10_02_patterned_recall(measured: dict[str, Any], kind: str) -> None:
    """IT10-02 recall >= 0.99 for each patterned type."""
    assert measured["recall"][kind] >= 0.99, measured["recall"]


def test_it10_02_name_recall(measured: dict[str, Any]) -> None:
    """IT10-02 directory PERSON recall >= 0.95."""
    assert measured["recall"]["PERSON"] >= 0.95


def test_it10_02_precision(measured: dict[str, Any]) -> None:
    """IT10-02 precision >= 0.90."""
    assert measured["precision"] >= 0.90


def test_it10_02_ticket_numbers_never_masked(measured: dict[str, Any]) -> None:
    """IT10-02 no INC/CHG (or PRB) ticket number is masked."""
    assert measured["tickets"] == []
