"""UT09-113: ``herness.reports._sample_policy`` keeps the T09-15c dashboard rule (T09-15d).

The case table copies ST09-30's inputs and expected keep/drop/redact decisions
(``tests/security/test_st09_evidence.py`` of T09-15c) so the report appendix and the dashboard
evidence widget cannot drift. Rows marked ``POST_RULING`` intentionally diverge from the
T09-15c dashboard (bc903cf): under the T09-15d controller ruling numeric cells (ints, floats,
decimal-shaped strings) pass ``redact_text`` instead of being shown verbatim, and are dropped
without a key. Post-ruling; dashboard must follow: ``app/common/_widgets_evidence.py`` imports
this policy (follow-up after batch 48); until then this table is the parity check.
"""

from __future__ import annotations

import json
from typing import Final

import pytest
from tests.support.report_env import install_redactor

from herness.core import redact
from herness.core.errors import ConfigError
from herness.reports import _sample_policy as policy

pytestmark = pytest.mark.unit

Q1: Final = "q_" + "a" * 16
PII: Final = "zq7-planted.pii@example.com"
RAW_TITLE: Final = f"PAY-1 refund for {PII}"
RUN: Final = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
ACCT: Final = "12345678901234567890123456"
PHONE: Final = 2025550123  # a phone stored as a JSON number
ACCT_F: Final = 2025550123.0  # an account-like value stored as a JSON float
VERBATIM: Final[dict[str, object]] = {
    "ok": True, "opened_at": "2026-01-02T03:04:05.000000Z", "day": "2026-01-02",
    "query_id": Q1, "run": RUN, "blank": None,
}  # fmt: skip
SAFE: Final[dict[str, object]] = {"n": 3, "amount": "12.50", **VERBATIM}
ALWAYS: Final = ("title", "summary", "description", "notes", "short_description", "close_notes")
NESTED: Final = {"k": "v", "Title": RAW_TITLE, "deep": [{"notes": RAW_TITLE}]}
POST_RULING: Final = "post-ruling; dashboard must follow"
SAME: Final = "same as T09-15c dashboard"

# (case, stored rows, columns with a key, values passed to redact_text in order,
#  columns without a key, parity note)
type Case = tuple[str, list[dict[str, object]], list[str], list[str], list[str], str]
CASES: Final[list[Case]] = [
    ("with_key", [
        {**SAFE, **dict.fromkeys(ALWAYS, RAW_TITLE), "comment": f"call {PII}"},
        {"n": 4, "comment": "second"},
        {"n": 5, "comment": None},
    ], [*SAFE, "comment"], ["3", "4", "5", "12.50", f"call {PII}", "second"],
     list(VERBATIM), POST_RULING),  # n, amount now redacted / dropped without a key
    ("digit_strings", [{"n": 1, "acct": ACCT, "ref": "acct_" + ACCT, "run": RUN}],
     ["n", "acct", "ref", "run"], ["1", ACCT, "acct_" + ACCT], ["run"], POST_RULING),
    ("name_variants_nested", [
        {" title": RAW_TITLE, "Title ": RAW_TITLE, "NOTES": RAW_TITLE, "n": 1, "meta": NESTED},
    ], ["n", "meta"], ["1", json.dumps({"k": "v", "deep": [{}]})], [], POST_RULING),
    ("text_only", [{"comment": f"call {PII}", "run": RUN, "title": RAW_TITLE}],
     ["comment", "run"], [f"call {PII}"], ["run"], SAME),
    ("numeric_pii", [{"phone": PHONE, "acct_f": ACCT_F, "dec": "555.0123", "at": "12:34:56",
                      "flag": False}],
     ["phone", "acct_f", "dec", "at", "flag"], ["2025550123", "2025550123.0", "555.0123"],
     ["at", "flag"], POST_RULING),
    ("pii_names", [{PII: "x", str(PHONE): "y", "n": 2, "day": "2026-01-02"}],
     ["n", "day"], [PII, str(PHONE), "2"], ["day"], POST_RULING),  # review I1: names checked
]  # fmt: skip
IDS: Final = [c[0] for c in CASES]
# Column names the double passes through unchanged (the real redactor keeps them as well).
NAMES: Final = frozenset(
    {k for c in CASES for row in c[1] for k in row if k not in {PII, str(PHONE)}}
    | {"meta", "d", "c"}
)


def _double(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ST09-30's redaction double: ``None`` (fail closed) for ``second``, else a counter.

    Known column names come back unchanged and are not recorded."""
    seen: list[str] = []

    def fake(text: str | None) -> str | None:
        assert text is not None
        if text in NAMES:
            return text
        seen.append(text)
        return None if text == "second" else f"redacted #{len(seen)}"

    monkeypatch.setattr(redact, "redact_text", fake)
    return seen


@pytest.mark.parametrize("entry", CASES, ids=IDS)
def test_ut09_113_parity_with_key(monkeypatch: pytest.MonkeyPatch, entry: Case) -> None:
    """UT09-113 cases with a key: always-drop columns gone, only non-verbatim cells redacted."""
    case, rows, columns, redacted, _keyless, _note = entry
    seen = _double(monkeypatch)
    out = policy.sample_rows(json.dumps(rows))
    assert out is not None
    assert [list(row) for row in out] == [columns] * len(rows)
    assert seen == redacted  # dropped and verbatim columns never reach redact_text
    text = json.dumps(out)
    for planted in (PII, RAW_TITLE, ACCT, str(PHONE), "555.0123"):
        assert planted not in text
    if case == "with_key":
        assert [row["comment"] for row in out] == ["redacted #5", "[redacted]", ""]
        assert [row["n"] for row in out] == ["redacted #1", "redacted #2", "redacted #3"]
        assert (out[0]["run"], out[0]["ok"], out[0]["blank"]) == (RUN, "True", "")
        assert out[0]["opened_at"] == SAFE["opened_at"]
    if case == "pii_names":
        assert out == [{"n": "redacted #3", "day": "2026-01-02"}]
    if case == "numeric_pii":
        assert (out[0]["at"], out[0]["flag"]) == ("12:34:56", "False")


@pytest.mark.parametrize("entry", CASES, ids=IDS)
def test_ut09_113_parity_without_key(monkeypatch: pytest.MonkeyPatch, entry: Case) -> None:
    """UT09-113 cases without a key: every non-verbatim column dropped, verbatim ones kept."""
    _case, rows, _columns, redacted, keyless, _note = entry

    def no_key() -> object:
        msg = "redact.hmac_key is not set"
        raise ConfigError(msg)

    monkeypatch.setattr(redact, "get_redactor", no_key)
    out = policy.sample_rows(rows)
    assert out is not None
    assert [list(row) for row in out] == [keyless] * len(rows)
    text = json.dumps(out)
    assert all(value not in text for value in redacted if len(value) > 4)  # not "3" in dates


def test_ut09_113_real_redactor_numbers(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-113 the impl 10 detectors mask phone-shaped numbers, keep counts and decimals."""
    install_redactor(monkeypatch)
    rows = [{"phone": PHONE, "acct_f": ACCT_F, "n": 42, "small": 7, "amount": "1200.50",
             "rate": 0.75, "neg": -0.5}]  # fmt: skip
    (out,) = policy.sample_rows(rows) or [{}]
    assert out["phone"].startswith("[PHONE_")
    assert out["acct_f"].startswith("[PHONE_")
    assert str(PHONE) not in json.dumps(out)
    assert (out["n"], out["small"], out["amount"], out["rate"], out["neg"]) == (
        "42",
        "7",
        "1200.50",
        "0.75",
        "-0.5",
    )


def test_ut09_113_real_redactor_column_names(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-113 (review I1) e-mail- and phone-shaped column names are dropped, real redactor."""
    install_redactor(monkeypatch)
    rows = [{PII: "x", str(PHONE): "y", "candidate_id": "epic-1", "value": 4.5}]
    assert policy.sample_rows(rows) == [{"candidate_id": "epic-1", "value": "4.5"}]


@pytest.mark.parametrize(
    ("name", "kept"),
    [("candidate_id", True), ("effort cost.usd-2", True), (PII, False), (str(PHONE), False),
     ("acct 12345", False), ("x" * 65, False), ("1st", False)],
)  # fmt: skip
def test_ut09_113_keyless_column_names(
    monkeypatch: pytest.MonkeyPatch, name: str, *, kept: bool
) -> None:
    """UT09-113 (review I1) without a key only identifier-shaped names without 5-digit runs."""

    def no_key() -> object:
        msg = "redact.hmac_key is not set"
        raise ConfigError(msg)

    monkeypatch.setattr(redact, "get_redactor", no_key)
    assert policy.sample_rows([{name: True}]) == [{name: "True"} if kept else {}]


def test_ut09_113_non_json_nested_value(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-113 (review M3) a non-JSON nested value is stringified, then redacted."""
    seen = _double(monkeypatch)
    assert policy.sample_rows([{"meta": {"k": {1, 2}}}]) == [{"meta": "redacted #1"}]
    assert seen == ['{"k": "{1, 2}"}']


def test_ut09_113_deep_nesting_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-113 a cell nested deeper than 32 levels is cut to ``null`` below the limit."""
    seen = _double(monkeypatch)
    cell: object = {"x": RAW_TITLE}
    for _ in range(40):
        cell = {"a": cell}
    assert policy.sample_rows([{"meta": cell}]) == [{"meta": "redacted #1"}]
    assert RAW_TITLE not in seen[0]
    assert seen[0].endswith("null" + "}" * 33)


def test_ut09_113_limit_cut_before_redaction(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-113 ``limit`` keeps the first rows before classification and redaction."""
    seen = _double(monkeypatch)
    rows = [{"d": "2026-01-01", "c": "a"}, {"d": "2026-01-02", "c": "b"}, {"d": "text", "c": "c"}]
    assert policy.sample_rows(rows, 2) == [{"d": "2026-01-01", "c": "redacted #1"},
                                           {"d": "2026-01-02", "c": "redacted #2"}]  # fmt: skip
    assert seen == ["a", "b"]
    assert policy.sample_rows(rows, 0) == []


@pytest.mark.parametrize("raw", [None, '{"a": 1}', "not json", 7])
def test_ut09_113_not_stored(raw: object) -> None:
    """UT09-113 NULL, unparsable or non-list samples are ``None`` ("Sample not stored")."""
    assert policy.sample_rows(raw) is None


def test_ut09_113_forbidden_columns_match_dashboard() -> None:
    """UT09-113 the policy's FORBIDDEN_TEXT_COLUMNS equal the dashboard guard's set."""
    from app.common import wh  # noqa: PLC0415 - the dashboard side of the parity check

    assert policy.FORBIDDEN_TEXT_COLUMNS == wh.FORBIDDEN_TEXT_COLUMNS
    assert {"title", "summary", "description", "notes"} <= policy.ALWAYS_DROPPED
