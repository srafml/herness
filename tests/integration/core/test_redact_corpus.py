"""IT10-02 redaction quality over the spec 11 labelled corpus (impl 10 T10-15, T10-11b, R-56).

Corpus: `write_pii_corpus(11, 5000, <tmp>)`; the redactor's name directory holds the corpus's
invented names as display names only (`First Last`): the `Last, First` and `F. Last` forms come
from U10-38 itself (T10-11b SN-2), not from `alt_names`. Matching rules, applied to
`Redactor.scan` output against the labels:

* a labelled span is *recalled* when a scanned span of the same type overlaps it (strict per
  type since T10-11b SN-1: `?token=` values are URL_TOKEN, no merged secret class);
* a labelled span is *exact* when a scanned span of its type has its exact boundaries. The
  corpus labels a CREDENTIAL as `password=<value>` and a URL_TOKEN as the whole URL while the
  spec masks only the value (U10-36: group 1, the query value), so for those two types the
  expected span is the text after the label's last `=`;
* a scanned span is a *true positive* when it overlaps any labelled span (any type), else a
  false positive; precision = true positives / scanned spans;
* "keep" ranges (INC/CHG/PRB ticket numbers; verbatim-safe shapes in the addendum rows) must
  not overlap any scanned span.

Thresholds (spec 10 IT10-02): recall >= 0.99 per patterned type, PERSON >= 0.95, precision
>= 0.90, zero masked ticket numbers. T10-11b ruling (the spec states no exact-span bound):
exact-span recall >= the same bounds (0.99 patterned, 0.95 PERSON), and neither table may fall
below its T10-11b floor (`RECALL_FLOOR`, `EXACT_FLOOR`). The addendum rows (T09-15d:
account-shaped digit runs as CARD, 7-digit local phones) are test-local
(`addendum_records`) and must be recalled exactly with every verbatim-safe value kept.
"""

from __future__ import annotations

import sys
from collections import Counter
from typing import Any

import pytest
from tests.support.redact_quality import (
    EXACT_FLOOR,
    RECALL_FLOOR,
    addendum_records,
    build_directory,
    load_corpus,
    make_redactor,
    write_corpus,
)

from herness.core.redact import Redactor

pytestmark = pytest.mark.integration

VALUE_ONLY = frozenset({"CREDENTIAL", "URL_TOKEN"})
PATTERNED = ("EMAIL", "PHONE", "IP", "EMPLOYEE_ID", "CARD", "CREDENTIAL", "URL_TOKEN")


def _expected(text: str, gold: dict[str, Any]) -> tuple[int, int]:
    start, end = gold["start"], gold["end"]
    if gold["type"] in VALUE_ONLY:
        start += text[start:end].rindex("=") + 1
    return start, end


def _measure(label: str, records: list[dict[str, Any]], red: Redactor) -> dict[str, Any]:
    total: Counter[str] = Counter()
    hit: Counter[str] = Counter()
    exact: Counter[str] = Counter()
    true_pos = scanned = 0
    masked_keep: list[str] = []
    for rec in records:
        text = rec["text"]
        found = red.scan(text)
        scanned += len(found)
        for gold in rec["spans"]:
            kind = gold["type"]
            total[kind] += 1
            same = [
                s
                for s in found
                if s.type == kind and s.start < gold["end"] and gold["start"] < s.end
            ]
            hit[kind] += bool(same)
            exact[kind] += any((s.start, s.end) == _expected(text, gold) for s in same)
        for s in found:
            true_pos += any(g["start"] < s.end and s.start < g["end"] for g in rec["spans"])
        masked_keep += [
            f"{rec['id']}:{k['start']}"  # position only: no value in test output
            for k in rec["keep"]
            if any(s.start < k["end"] and k["start"] < s.end for s in found)
        ]
    recall = {t: hit[t] / total[t] for t in total}
    exact_rate = {t: exact[t] / total[t] for t in total}
    precision = true_pos / scanned
    per_type = " ".join(
        f"{t}={recall[t]:.4f}(n={total[t]},exact={exact_rate[t]:.4f})" for t in sorted(total)
    )
    sys.stderr.write(
        f"IT10-02 {label} recall {per_type} precision={precision:.4f} (tp={true_pos}, "
        f"scanned={scanned}) masked_keep={len(masked_keep)}\n"
    )
    return {
        "recall": recall,
        "exact": exact_rate,
        "total": total,
        "precision": precision,
        "kept": masked_keep,
    }


@pytest.fixture(scope="module")
def measured(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("pii_corpus")
    records = load_corpus(write_corpus(work))
    return _measure("corpus", records, make_redactor(build_directory(work)))


@pytest.fixture(scope="module")
def addendum(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    work = tmp_path_factory.mktemp("pii_addendum")
    return _measure("addendum", addendum_records(), make_redactor(build_directory(work)))


def test_it10_02_corpus_has_every_type(measured: dict[str, Any]) -> None:
    """The corpus exercises every patterned type and PERSON (guards against vacuous recall)."""
    for kind in (*PATTERNED, "PERSON"):
        assert measured["total"][kind] >= 100, kind


@pytest.mark.parametrize("kind", PATTERNED)
def test_it10_02_patterned_recall(measured: dict[str, Any], kind: str) -> None:
    """IT10-02 strict same-type recall >= 0.99 for each patterned type."""
    assert measured["recall"][kind] >= 0.99, measured["recall"]


def test_it10_02_url_token_strict_recall(measured: dict[str, Any]) -> None:
    """IT10-02 URL_TOKEN values are recalled as URL_TOKEN, no merged secret class (SN-1)."""
    assert measured["recall"]["URL_TOKEN"] >= 0.99


def test_it10_02_name_recall(measured: dict[str, Any]) -> None:
    """IT10-02 directory PERSON recall >= 0.95 with display names only (SN-2)."""
    assert measured["recall"]["PERSON"] >= 0.95


@pytest.mark.parametrize("kind", [*PATTERNED, "PERSON"])
def test_it10_02_exact_span_recall(measured: dict[str, Any], kind: str) -> None:
    """IT10-02 exact-span recall >= 0.99 per patterned type and >= 0.95 PERSON (T10-11b)."""
    assert measured["exact"][kind] >= (0.95 if kind == "PERSON" else 0.99), measured["exact"]


def test_it10_02_recall_tables_do_not_regress(measured: dict[str, Any]) -> None:
    """IT10-02 the per-type recall and exact-span tables stay at or above their floors."""
    assert {k: v for k, v in RECALL_FLOOR.items() if measured["recall"][k] < v} == {}
    assert {k: v for k, v in EXACT_FLOOR.items() if measured["exact"][k] < v} == {}


def test_it10_02_precision(measured: dict[str, Any]) -> None:
    """IT10-02 precision >= 0.90."""
    assert measured["precision"] >= 0.90


def test_it10_02_ticket_numbers_never_masked(measured: dict[str, Any]) -> None:
    """IT10-02 no INC/CHG (or PRB) ticket number is masked."""
    assert measured["kept"] == []


def test_it10_02_addendum_rows(addendum: dict[str, Any]) -> None:
    """IT10-02 account-shaped runs (CARD) and local phones are recalled exactly; no
    verbatim-safe value (build id, ULID, date, timestamp, time, query id, number) is masked."""
    assert addendum["total"] == {"CARD": 400, "PHONE": 400}
    assert addendum["recall"] == addendum["exact"] == {"CARD": 1.0, "PHONE": 1.0}
    assert addendum["precision"] == 1.0
    assert addendum["kept"] == []
