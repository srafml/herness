"""UT09-113: ``herness.reports._sample_policy`` keeps the T09-15c dashboard rule (T09-15d).

The case table copies ST09-30's inputs and expected keep/drop/redact decisions
(``tests/security/test_st09_evidence.py`` of T09-15c) so the report appendix and the dashboard
evidence widget cannot drift. Follow-up after batch 48: ``app/common/_widgets_evidence.py``
imports this policy; until then this table is the parity check.
"""

from __future__ import annotations

import json
from typing import Final

import pytest

from herness.core import redact
from herness.core.errors import ConfigError
from herness.reports import _sample_policy as policy

pytestmark = pytest.mark.unit

Q1: Final = "q_" + "a" * 16
PII: Final = "zq7-planted.pii@example.com"
RAW_TITLE: Final = f"PAY-1 refund for {PII}"
RUN: Final = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
ACCT: Final = "12345678901234567890123456"
SAFE: Final[dict[str, object]] = {
    "n": 3, "amount": "12.50", "ok": True, "opened_at": "2026-01-02T03:04:05.000000Z",
    "day": "2026-01-02", "query_id": Q1, "run": RUN, "blank": None,
}  # fmt: skip
ALWAYS: Final = ("title", "summary", "description", "notes", "short_description", "close_notes")
NESTED: Final = {"k": "v", "Title": RAW_TITLE, "deep": [{"notes": RAW_TITLE}]}

# (case, stored rows, expected columns with a key, values passed to redact_text in order)
CASES: Final[list[tuple[str, list[dict[str, object]], list[str], list[str]]]] = [
    ("with_key", [
        {**SAFE, **dict.fromkeys(ALWAYS, RAW_TITLE), "comment": f"call {PII}"},
        {"n": 4, "comment": "second"},
        {"n": 5, "comment": None},
    ], [*SAFE, "comment"], [f"call {PII}", "second"]),
    ("digit_strings", [{"n": 1, "acct": ACCT, "ref": "acct_" + ACCT, "run": RUN}],
     ["n", "acct", "ref", "run"], [ACCT, "acct_" + ACCT]),
    ("name_variants_nested", [
        {" title": RAW_TITLE, "Title ": RAW_TITLE, "NOTES": RAW_TITLE, "n": 1, "meta": NESTED},
    ], ["n", "meta"], [json.dumps({"k": "v", "deep": [{}]})]),
]  # fmt: skip


def _double(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """ST09-30's redaction double: ``None`` (fail closed) for ``second``, else a counter."""
    seen: list[str] = []

    def fake(text: str | None) -> str | None:
        assert text is not None
        seen.append(text)
        return None if text == "second" else f"redacted #{len(seen)}"

    monkeypatch.setattr(redact, "redact_text", fake)
    return seen


@pytest.mark.parametrize(("case", "rows", "columns", "redacted"), CASES, ids=[c[0] for c in CASES])
def test_ut09_113_parity_with_key(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    rows: list[dict[str, object]],
    columns: list[str],
    redacted: list[str],
) -> None:
    """UT09-113 ST09-30 cases with a key: always-drop columns gone, only text redacted."""
    seen = _double(monkeypatch)
    out = policy.sample_rows(json.dumps(rows))
    assert out is not None
    assert [list(row) for row in out] == [columns] * len(rows)
    assert seen == redacted  # dropped and typed columns never reach redact_text
    text = json.dumps(out)
    assert PII not in text
    assert RAW_TITLE not in text
    assert ACCT not in text
    if case == "with_key":
        assert [row["comment"] for row in out] == ["redacted #1", "[redacted]", ""]
        assert (out[0]["run"], out[0]["ok"], out[0]["n"]) == (RUN, "True", "3")
        assert out[0]["opened_at"] == SAFE["opened_at"]
        assert out[0]["blank"] == ""


@pytest.mark.parametrize(("case", "rows", "columns", "redacted"), CASES, ids=[c[0] for c in CASES])
def test_ut09_113_parity_without_key(
    monkeypatch: pytest.MonkeyPatch,
    case: str,
    rows: list[dict[str, object]],
    columns: list[str],
    redacted: list[str],
) -> None:
    """UT09-113 ST09-30 cases without a key: every text column dropped, typed ones kept."""

    def no_key() -> object:
        msg = "redact.hmac_key is not set"
        raise ConfigError(msg)

    monkeypatch.setattr(redact, "get_redactor", no_key)
    out = policy.sample_rows(rows)
    assert out is not None
    kept = [c for c in columns if c not in {"comment", "acct", "ref", "meta"}]
    assert [list(row) for row in out] == [kept] * len(rows)
    assert "second" not in json.dumps(out)
    assert all(text not in json.dumps(out) for text in redacted)
    if case == "with_key":
        assert [row["n"] for row in out] == ["3", "4", "5"]


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
    rows = [{"n": 1, "c": "a"}, {"n": 2, "c": "b"}, {"n": "free text", "c": "c"}]
    assert policy.sample_rows(rows, 2) == [{"n": "1", "c": "redacted #1"},
                                           {"n": "2", "c": "redacted #2"}]  # fmt: skip
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
