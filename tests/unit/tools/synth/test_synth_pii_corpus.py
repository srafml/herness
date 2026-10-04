"""Tests for tools.synth.pii_corpus (U11-25): UT11-66."""

import itertools
import json
import re
from collections import Counter
from pathlib import Path
from typing import Any

import pytest

from tools.synth.params import SynthUsageError
from tools.synth.pii import build_name_list
from tools.synth.pii_corpus import write_pii_corpus

pytestmark = pytest.mark.unit

_TYPES = ("PERSON", "EMAIL", "PHONE", "IP", "EMPLOYEE_ID", "CARD", "CREDENTIAL", "URL_TOKEN")
_TICKET = re.compile(r"(INC|CHG|PRB)\d{7}")
_FIRSTS = {first for first, _ in build_name_list(11)}
_LASTS = {last for _, last in build_name_list(11)}
_RULES = {
    "EMAIL": r"[a-z]+\.[a-z]+@example\.(com|org)",
    "PHONE": r"\+1-202-555-01\d\d|\+1 202 555 01\d\d|\(202\) 555-01\d\d",
    "IP": r"(192\.0\.2|198\.51\.100)\.\d{1,3}",
    "EMPLOYEE_ID": r"E\d{6}",
    "CARD": r"4111\d{12}",
    "CREDENTIAL": r"password=synthetic[A-Za-z0-9]{8}",
    "URL_TOKEN": r"https://portal\.example\.com/x\?token=synthetic[0-9a-f]{16}",
}


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> list[dict[str, Any]]:
    out = tmp_path_factory.mktemp("corpus") / "pii_corpus.jsonl"
    assert write_pii_corpus(11, 2000, out) == 2000
    lines = out.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


def _name_format(value: str) -> str:
    """The name format of a PERSON value; its parts come from the synthetic name list."""
    if ", " in value:
        last, first = value.split(", ")
        assert first in _FIRSTS, value
        assert last in _LASTS, value
        return "last_first"
    head, last = value.split(" ")
    assert last in _LASTS, value
    if re.fullmatch(r"[A-Z]\.", head):
        return "initial_last"
    assert head in _FIRSTS, value
    return "first_last"


def test_ut11_66_corpus_has_n_lines_with_the_record_shape(corpus: list[dict[str, Any]]) -> None:
    """UT11-66 `write_pii_corpus(11, 2000, out)` writes 2,000 JSONL records with ids
    `pii-<nnnnnn>`, spans and keep entries that index the text and never overlap."""
    assert len(corpus) == 2000
    assert [r["id"] for r in corpus] == [f"pii-{i:06d}" for i in range(1, 2001)]
    for record in corpus:
        assert set(record) == {"id", "text", "spans", "keep"}
        text = record["text"]
        marks = sorted((s["start"], s["end"]) for s in record["spans"] + record["keep"])
        for left, right in itertools.pairwise(marks):
            assert left[1] <= right[0]
        for span in record["spans"]:
            assert set(span) == {"start", "end", "type"}
            assert span["type"] in _TYPES
            assert 0 <= span["start"] < span["end"] <= len(text)
        for keep in record["keep"]:
            assert set(keep) == {"start", "end", "kind"}
            assert keep["kind"] == "ticket_number"
        found = {(m.start(), m.end()) for m in _TICKET.finditer(text)}
        assert found == {(k["start"], k["end"]) for k in record["keep"]}


def test_ut11_66_composition_rules_hold(corpus: list[dict[str, Any]]) -> None:
    """UT11-66 composition: >= 30 % of records carry a PERSON span and all 3 name formats
    occur; every type is in >= 5 % of records; >= 20 % keep a ticket number; about 10 %
    carry no span; 0..3 spans per record."""
    n = len(corpus)
    with_type = Counter(t for r in corpus for t in {s["type"] for s in r["spans"]})
    formats = Counter(
        _name_format(r["text"][s["start"] : s["end"]])
        for r in corpus
        for s in r["spans"]
        if s["type"] == "PERSON"
    )
    assert with_type["PERSON"] >= 0.30 * n
    assert set(formats) == {"first_last", "last_first", "initial_last"}
    for kind in _TYPES:
        assert with_type[kind] >= 0.05 * n, kind
    assert sum(1 for r in corpus if r["keep"]) >= 0.20 * n
    empty = sum(1 for r in corpus if not r["spans"])
    assert 0.08 * n <= empty <= 0.12 * n
    assert Counter(len(r["spans"]) for r in corpus).keys() == {0, 1, 2, 3}


def test_ut11_66_values_are_synthetic_by_construction(corpus: list[dict[str, Any]]) -> None:
    """UT11-66 / TH11-01 every injected value comes from a reserved or synthetic range;
    all three phone formats occur."""
    phones: set[str] = set()
    for record in corpus:
        for span in record["spans"]:
            value = record["text"][span["start"] : span["end"]]
            if span["type"] == "PERSON":
                _name_format(value)
                continue
            assert re.fullmatch(_RULES[span["type"]], value), (span["type"], value)
            if span["type"] == "PHONE":
                phones.add(value[:3])
    assert phones == {"+1-", "+1 ", "(20"}


def test_ut11_66_same_seed_same_bytes(tmp_path: Path) -> None:
    """UT11-66 the corpus is a function of the seed; the atomic write leaves no temp file."""
    first, second = tmp_path / "a" / "c.jsonl", tmp_path / "b" / "c.jsonl"
    write_pii_corpus(5, 2000, first)
    write_pii_corpus(5, 2000, second)
    assert first.read_bytes() == second.read_bytes()
    assert [p.name for p in first.parent.iterdir()] == ["c.jsonl"]


@pytest.mark.parametrize("n", [100, 1999, 1_000_001, True])
def test_ut11_66_out_of_range_n_raises(tmp_path: Path, n: int) -> None:
    """UT11-66 `n = 100` (and any n outside 2,000..1,000,000) raises `SynthUsageError`."""
    out = tmp_path / "c.jsonl"
    with pytest.raises(SynthUsageError):
        write_pii_corpus(11, n, out)
    assert not out.exists()
