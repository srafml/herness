"""ST09-30: no raw text from a stored ``result_sample`` reaches the evidence widget (TH09-11).

T09-15c/T09-15e: ``title``, ``summary``, ``description``, ``notes`` and the FORBIDDEN_TEXT_COLUMNS
are always dropped; every other column, numeric ones included, is redacted with the loaded key or
dropped when no key is loaded; only boolean, date/time, id and NULL columns are shown as stored.
Column names are kept only when ``redact_text`` leaves them unchanged (identifier-shaped with at
most four digits without a key). The dashboard shares ``herness.reports._sample_policy``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import preserved_main
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from app.common import data, wh
from app.common.wh import QueryResult
from herness.core import config, redact
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

Q1 = "q_" + "a" * 16
PII = "zq7-planted.pii@example.com"
RAW_TITLE = f"PAY-1 refund for {PII}"
RUN = "run_01J8ZZZZZZZZZZZZZZZZZZZZZZ"
SAFE = {
    "n": 3, "amount": "12.50", "ok": True, "opened_at": "2026-01-02T03:04:05.000000Z",
    "day": "2026-01-02", "query_id": Q1, "run": RUN, "blank": None,
}  # fmt: skip
ALWAYS_DROPPED = ("title", "summary", "description", "notes", "short_description", "close_notes")


def _sample() -> list[dict[str, object]]:
    row: dict[str, object] = dict.fromkeys(ALWAYS_DROPPED, RAW_TITLE)
    return [
        {**SAFE, **row, "comment": f"call {PII}"},
        {"n": 4, "comment": "second"},
        {"n": 5, "comment": None},
    ]


def _plant(monkeypatch: pytest.MonkeyPatch, sample: list[dict[str, object]]) -> None:
    """Store ``sample`` as the ``meta.evidence`` result_sample of ``Q1``."""
    meta = pa.Table.from_pylist([{
        "query_id": Q1, "sql": "SELECT 1", "params": "{}", "row_count": 2, "executed_at": None,
        "result_sample": json.dumps(sample),
    }])  # fmt: skip
    monkeypatch.setattr(
        wh, "query", lambda *_a, **_k: QueryResult(meta, truncated=False, build_id="b")
    )


@pytest.fixture(autouse=True)
def _isolated(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    monkeypatch.setattr(data, "ops_read", lambda *_a, **_k: {})
    _plant(monkeypatch, _sample())
    with preserved_main():
        yield
    st.cache_data.clear()
    config.reset_config()


def evidence_script() -> None:
    from app.common.widgets import evidence  # noqa: PLC0415

    evidence(["q_" + "a" * 16])


def _rendered(at: AppTest) -> str:
    """Every rendered text, including the expander label and all dataframe cells and columns."""
    kinds = ("markdown", "caption", "text", "code", "json", "subheader", "error", "warning")
    parts = [str(e.value) for kind in kinds for e in getattr(at.main, kind)]
    parts += [e.label for e in at.expander]
    parts += [d.value.to_csv() for d in at.dataframe]
    return "\n".join(parts)


def _redaction_double(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    seen: list[str] = []
    names = {*SAFE, "comment", "acct", "ref", "meta"}  # column names stay unchanged

    def fake(text: str | None) -> str | None:
        assert text is not None
        if text in names:
            return text
        seen.append(text)
        return None if text == "second" else f"redacted #{len(seen)}"

    monkeypatch.setattr(redact, "redact_text", fake)
    return seen


def test_st09_30_raw_title_never_rendered_with_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 key loaded: raw title and planted PII absent; free text only redacted."""
    seen = _redaction_double(monkeypatch)
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    text = _rendered(at)
    assert PII not in text
    assert RAW_TITLE not in text
    assert "PAY-1" not in text
    frame = at.dataframe[0].value
    assert list(frame.columns) == [*SAFE, "comment"]
    assert list(frame["comment"]) == [
        "redacted #5",
        "[redacted]",
        "",
    ]  # None from redaction; empty cell
    assert list(frame["n"]) == ["redacted #1", "redacted #2", "redacted #3"]  # numbers redacted
    assert list(frame["amount"]) == ["redacted #4", "", ""]
    assert seen == ["3", "4", "5", "12.50", f"call {PII}", "second"]  # dropped/id columns never
    assert frame.iloc[0]["run"] == RUN
    assert frame.iloc[0]["ok"] == "True"
    assert frame.iloc[0]["opened_at"] == SAFE["opened_at"]


def test_st09_30_raw_title_never_rendered_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 no key loaded: text and numeric columns dropped; verbatim-safe columns shown."""

    def no_key() -> object:
        msg = "redact.hmac_key is not set"
        raise ConfigError(msg)

    monkeypatch.setattr(redact, "get_redactor", no_key)
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    text = _rendered(at)
    assert PII not in text
    assert RAW_TITLE not in text
    assert "second" not in text
    frame = at.dataframe[0].value
    assert list(frame.columns) == [k for k in SAFE if k not in {"n", "amount"}]
    assert frame.iloc[0]["ok"] == "True"


def test_st09_30_digit_strings_are_text_not_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 a 26-digit account number (bare or ``prefix_``) is redacted, never shown raw."""
    acct = "12345678901234567890123456"
    _plant(monkeypatch, [{"n": 1, "acct": acct, "ref": "acct_" + acct, "run": RUN}])
    seen = _redaction_double(monkeypatch)
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    assert acct not in _rendered(at)
    assert seen == ["1", acct, "acct_" + acct]  # the ULID-shaped run id stays an id
    assert list(at.dataframe[0].value["run"]) == [RUN]


def test_st09_30_name_variants_and_nested_keys_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 `` title``, ``Title ``, ``NOTES`` dropped; nested always-drop keys removed."""
    nested = {"k": "v", "Title": RAW_TITLE, "deep": [{"notes": RAW_TITLE}]}
    row = {" title": RAW_TITLE, "Title ": RAW_TITLE, "NOTES": RAW_TITLE, "n": 1, "meta": nested}
    _plant(monkeypatch, [row])
    seen = _redaction_double(monkeypatch)
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    assert list(at.dataframe[0].value.columns) == ["n", "meta"]
    assert seen == ["1", json.dumps({"k": "v", "deep": [{}]})]
    assert PII not in _rendered(at)


def test_st09_30_real_redactor_with_loaded_key(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """ST09-30 end to end: a real test HMAC key and the real redactor (no double)."""
    config.reset_config()
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    config.init_config("local", config_dir=write_full_config(tmp_path), env={})
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    text = _rendered(at)
    assert PII not in text
    assert RAW_TITLE not in text
    assert "PAY-1" not in text
    frame = at.dataframe[0].value
    assert list(frame.columns) == [*SAFE, "comment"]
    assert frame.iloc[0]["comment"].startswith("call [EMAIL_")
    assert frame.iloc[1]["comment"] == "second"
    assert list(frame["n"]) == ["3", "4", "5"]  # counts pass the real redactor unchanged
    assert frame.iloc[0]["amount"] == "12.50"


def test_st09_30_real_redactor_numbers_and_names(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """ST09-30 real redactor: a phone-like number is masked; PII-shaped column names dropped."""
    config.reset_config()
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    config.init_config("local", config_dir=write_full_config(tmp_path), env={})
    _plant(monkeypatch, [{"phone": 2025550123, "n": 7, PII: "x", "2025550123": "y"}])
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    frame = at.dataframe[0].value
    assert list(frame.columns) == ["phone", "n"]
    assert frame.iloc[0]["phone"].startswith("[PHONE_")
    assert frame.iloc[0]["n"] == "7"
    assert "2025550123" not in _rendered(at)
    assert PII not in _rendered(at)


def test_st09_30_column_names_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 no key: only identifier-shaped names with at most four digits are kept."""

    def no_key() -> object:
        msg = "redact.hmac_key is not set"
        raise ConfigError(msg)

    monkeypatch.setattr(redact, "get_redactor", no_key)
    _plant(monkeypatch, [{"ok": True, "v1234": False, PII: True, "2025550123": True}])
    at = AppTest.from_function(evidence_script).run()
    assert not at.exception
    assert list(at.dataframe[0].value.columns) == ["ok", "v1234"]


def test_st09_30_deep_nesting_cut(monkeypatch: pytest.MonkeyPatch) -> None:
    """ST09-30 a cell nested deeper than 32 levels is cut to ``null`` below the limit."""
    from app.common._widgets_evidence import sample_rows  # noqa: PLC0415

    seen = _redaction_double(monkeypatch)
    cell: object = {"x": RAW_TITLE}
    for _ in range(40):
        cell = {"a": cell}
    rows = sample_rows([{"meta": cell}])
    assert rows == [{"meta": "redacted #1"}]
    assert RAW_TITLE not in seen[0]
    assert seen[0].endswith("null" + "}" * 33)
