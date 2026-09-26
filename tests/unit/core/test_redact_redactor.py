"""Tests for the Redactor, get_redactor and redact_text (impl 10 T10-10, U10-40 to U10-48).

Secret-looking fixtures (keys, ``api_key=...`` values) are built at runtime so the
detect-secrets baseline does not change.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import types
from collections.abc import Iterator
from itertools import pairwise
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import redact as r
from herness.core import redact_patterns as rp
from herness.core.errors import ConfigError, FatalError
from herness.core.logging import configure_logging, reset_logging
from herness.core.redact_directory import NameDirectory
from herness.core.settings import RedactionConfig

pytestmark = pytest.mark.unit

SVC = "herness"
KEY_A = bytes(range(32))
KEY_B = bytes(range(1, 33))
TOKEN_RE = re.compile(r"^\[[A-Z_]+_[0-9a-f]{10}\]$")
KEY_PARAM = "api" + "_key"  # fixtures built at runtime keep detect-secrets quiet
PLANTED = "Zq" + "9xT4" + "mW2pLk"
BLOCKING = frozenset(
    {
        "EMAIL",
        "PHONE",
        "CARD",
        "NATIONAL_ID",
        "CREDENTIAL",
        "URL_TOKEN",
        "EMPLOYEE_ID",
        "USER_ID",
        "PERSON",
        "IP",  # mask_ip is true in these tests
    }
)


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    """In-memory keyring; no cached config, redactor or logging survives a test."""
    c.reset_config()
    yield
    c.reset_config()
    reset_logging()


def _redactor(
    key: bytes = KEY_A, *, names: tuple[str, ...] = ("Jane Doe",), **cfg: Any
) -> r.Redactor:
    directory = NameDirectory.from_files(None, names, None)
    return r.Redactor(RedactionConfig(directory_file=None, **cfg), key, directory)


def _err_lines(capsys: pytest.CaptureFixture[str]) -> list[dict[str, Any]]:
    return [json.loads(text) for text in capsys.readouterr().err.splitlines() if text.strip()]


def _init_config(tmp_path: Path, directory_file: Path | None = None) -> c.HernessConfig:
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    value = "null" if directory_file is None else json.dumps(directory_file.as_posix())
    text = herness.read_text("utf-8").replace(
        "redaction: {directory_file: null}", f"redaction: {{directory_file: {value}}}"
    )
    herness.write_text(text, "utf-8")
    return c.init_config("local", config_dir=cfg_dir, env={})


# --- UT10-37 scan overlap: earlier type wins ---------------------------------------------


def test_ut10_37_email_inside_url_token_is_one_url_token_span() -> None:
    """UT10-37 an e-mail as a URL query value is one URL_TOKEN span; no EMAIL overlaps."""
    text = "see https://h.example/cb?code=alice@example.com&x=1 now"
    spans = _redactor().scan(text)
    assert [(s.type, text[s.start : s.end]) for s in spans] == [("URL_TOKEN", "alice@example.com")]
    assert spans[0].replacement == "[SECRET]"


def test_ut10_37_token_param_credential_wins_over_url_token_and_email() -> None:
    """UT10-37 `token=` inside a URL is caught by CREDENTIAL first; no other span overlaps."""
    text = "https://h.example/p?token=bob@example.com"
    spans = _redactor().scan(text)
    assert [s.type for s in spans] == ["CREDENTIAL"]
    assert text[spans[0].start : spans[0].end] == "bob@example.com"


def test_ut10_37_card_inside_credential_is_credential_only() -> None:
    """UT10-37 a Luhn-valid card as a password value is CREDENTIAL, not CARD."""
    text = "password=4111111111111111 and card 4111 1111 1111 1111"
    spans = _redactor().scan(text)
    assert [s.type for s in spans] == ["CREDENTIAL", "CARD"]
    assert spans == sorted(spans)
    for left, right in pairwise(spans):
        assert left.end <= right.start


def test_ut10_37_scan_oversize_and_detector_failure_raise_redaction_failed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT10-37 text > 4,000,000 chars -> `text too long`; a detector error names its type."""
    red = _redactor()
    with pytest.raises(r.RedactionFailed, match=r"^text too long$"):
        red.scan("a" * 4_000_001)

    def boom(self: NameDirectory, text: str) -> list[tuple[int, int, str]]:
        raise RuntimeError(text)

    monkeypatch.setattr(NameDirectory, "find", boom)
    with pytest.raises(r.RedactionFailed, match=r"^PERSON$") as exc:
        red.scan("hello Jane Doe")
    assert "Jane" not in str(exc.value)
    assert issubclass(r.RedactionFailed, FatalError)


# --- UT10-39 PERSON pseudonyms ---------------------------------------------------------------


def test_ut10_39_person_variants_share_one_token(tmp_path: Path) -> None:
    """UT10-39 `Jane Doe`, `Doe, Jane`, `JANE  DOE` -> one `[PERSON_<10 hex>]` token."""
    csv_path = tmp_path / "directory.csv"
    csv_path.write_text("display_name\nJane Doe\n", "utf-8")
    directory = NameDirectory.from_files(csv_path, ("JANE  DOE",), None)
    red = r.Redactor(RedactionConfig(directory_file=None), KEY_A, directory)
    result = red.redact("Jane Doe; Doe, Jane; JANE  DOE.")
    assert result is not None
    tokens = re.findall(r"\[[A-Z_]+_[0-9a-f]{10}\]", result.text)
    assert len(tokens) == 3
    assert len(set(tokens)) == 1
    assert tokens[0].startswith("[PERSON_")
    assert TOKEN_RE.match(tokens[0])
    assert result.counts == {"PERSON": 3}
    assert red.pseudonym("PERSON", "JANE  DOE") == tokens[0]
    assert red.pseudonym("PERSON", "Doe, Jane") == tokens[0]


# --- UT10-40 key handling ---------------------------------------------------------------------


def test_ut10_40_two_keys_give_different_tokens_and_key_id() -> None:
    """UT10-40 same value under two keys -> different tokens; key_id is the SHA-256 prefix."""
    a, b = _redactor(KEY_A), _redactor(KEY_B)
    assert a.pseudonym("EMAIL", "x@example.com") != b.pseudonym("EMAIL", "x@example.com")
    assert a.key_id == hashlib.sha256(KEY_A).hexdigest()[:8]
    assert b.key_id == hashlib.sha256(KEY_B).hexdigest()[:8]
    assert repr(a) == f"Redactor(key_id='{a.key_id}')"
    assert KEY_A.hex() not in repr(a)


def test_ut10_40_31_byte_key_is_config_error() -> None:
    """UT10-40 a 31-byte key raises ConfigError('redaction key must be 32 bytes')."""
    with pytest.raises(ConfigError, match=r"^redaction key must be 32 bytes$"):
        r.Redactor(RedactionConfig(directory_file=None), bytes(31))


def test_ut10_40_get_redactor_builds_caches_and_logs(
    tmp_path: Path, fake_keyring: MemoryKeyring, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-40 get_redactor loads the key and directory once, logs, and resets with config."""
    csv_path = tmp_path / "directory.csv"
    csv_path.write_text("display_name,alt_names\nMaria Garcia,M Garcia\n", "utf-8")
    fake_keyring.store[(SVC, "redact.hmac_key")] = KEY_A.hex()
    _init_config(tmp_path, csv_path)
    configure_logging("INFO")
    capsys.readouterr()
    red = r.get_redactor()
    assert red is r.get_redactor()
    assert red.key_id == hashlib.sha256(KEY_A).hexdigest()[:8]
    loaded = [line for line in _err_lines(capsys) if line["event"] == "redact.directory.loaded"]
    assert len(loaded) == 1
    assert loaded[0]["names"] == 1
    assert loaded[0]["variants"] == 4
    assert isinstance(loaded[0]["duration_ms"], int)
    result = red.redact("ask Maria Garcia or M Garcia")
    assert result is not None
    assert result.counts == {"PERSON": 2}
    c.reset_config()
    _init_config(tmp_path / "again")
    assert r.get_redactor() is not red


@pytest.mark.parametrize("value", [None, "abc", "zz" * 32, "0" * 63, " " + "0" * 64])
def test_ut10_40_get_redactor_rejects_missing_or_malformed_key(
    tmp_path: Path, fake_keyring: MemoryKeyring, value: str | None
) -> None:
    """UT10-40 a missing key or one that is not 64 hex characters raises ConfigError."""
    _init_config(tmp_path)  # before the key: a malformed key also fails config_hash at load
    if value is not None:
        fake_keyring.store[(SVC, "redact.hmac_key")] = value
    expected = r"^secret not found: " if value is None else r"^redact\.hmac_key must be 64 hex"
    with pytest.raises(ConfigError, match=expected):
        r.get_redactor()


def test_ut10_40_key_id_provider_feeds_config_hash(
    tmp_path: Path, fake_keyring: MemoryKeyring
) -> None:
    """UT10-40 the registered _KEY_ID_PROVIDER gives the key id; a missing key is unresolved."""
    cfg = _init_config(tmp_path)
    assert c._KEY_ID_PROVIDER is r._config_key_id
    assert r.reset_redactor in c._RESET_HOOKS
    assert c.config_hash(cfg) == c.config_hash(cfg, key_id="unresolved")
    fake_keyring.store[(SVC, "redact.hmac_key")] = KEY_B.hex()
    key_id = hashlib.sha256(KEY_B).hexdigest()[:8]
    assert r._config_key_id(cfg) == key_id
    assert c.config_hash(cfg) == c.config_hash(cfg, key_id=key_id)
    assert c.config_hash(cfg) != c.config_hash(cfg, key_id="unresolved")


# --- UT10-41 mask_ip --------------------------------------------------------------------------


def test_ut10_41_mask_ip_false_keeps_ip_but_scan_reports_it() -> None:
    """UT10-41 with mask_ip false the IP stays in the text; scan still reports an IP span."""
    text = "host 10.1.2.3 down"
    kept = _redactor(mask_ip=False)
    result = kept.redact(text)
    assert result == r.RedactionResult(text, {})
    assert [s.type for s in kept.scan(text)] == ["IP"]
    masked = _redactor().redact(text)
    assert masked is not None
    assert "10.1.2.3" not in masked.text
    assert masked.counts == {"IP": 1}


# --- UT10-42 None, empty, failure -------------------------------------------------------------


def test_ut10_42_none_and_empty() -> None:
    """UT10-42 redact(None) -> None, redact('') -> empty result; redact_text(None) -> None."""
    red = _redactor()
    assert red.redact(None) is None
    assert red.redact("") == r.RedactionResult("", {})
    assert r.redact_text(None) is None


def test_ut10_42_detector_raising_fails_closed_and_logs(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT10-42 a raising detector: redact raises; redact_text returns None and logs no text."""
    red = _redactor()
    monkeypatch.setattr(r._State, "redactor", red)

    def boom(self: NameDirectory, text: str) -> list[tuple[int, int, str]]:
        raise ValueError(text)

    monkeypatch.setattr(NameDirectory, "find", boom)
    with pytest.raises(r.RedactionFailed, match=r"^PERSON$"):
        red.redact("mail Jane Doe")
    configure_logging("INFO")
    capsys.readouterr()
    assert r.redact_text("mail Jane Doe at jane@example.com") is None
    captured = capsys.readouterr().err
    lines = [json.loads(line) for line in captured.splitlines() if line.strip()]
    failed = [line for line in lines if line["event"] == "redact.record.failed"]
    assert len(failed) == 1
    assert failed[0]["level"] == "warning"
    assert failed[0]["error_type"] == "RedactionFailed"
    assert failed[0]["reason"] == "PERSON"
    assert "jane" not in captured.lower()


def test_ut10_42_redact_text_removes_email_and_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-42 redact_text removes an e-mail and an `api_key=` value (T08-04 contract)."""
    monkeypatch.setattr(r._State, "redactor", _redactor())
    text = f"from ops@example.com: {KEY_PARAM}={PLANTED} failed"
    out = r.redact_text(text)
    assert out is not None
    assert "ops@example.com" not in out
    assert PLANTED not in out
    assert f"{KEY_PARAM}=[SECRET]" in out
    assert re.search(r"\[EMAIL_[0-9a-f]{10}\]", out)


# --- UT10-45 protected tokens ------------------------------------------------------------------


def test_ut10_45_existing_tokens_are_not_detected() -> None:
    """UT10-45 `[PERSON_0123456789]` and `[SECRET]` are protected (digit-only hex not a phone)."""
    text = "user [PERSON_0123456789] key [SECRET] ip [IP_abcdef0123]"
    red = _redactor()
    assert red.scan(text) == []
    assert red.redact(text) == r.RedactionResult(text, {})


# --- RF: adjacent numeric entities (CARD cuts, masked detector view) ----------------------------

_CARD_NO = "4111 1111 1111 1111"
_ADJACENT = [
    (f"{_CARD_NO} 10.20.30.40", [("CARD", _CARD_NO), ("IP", "10.20.30.40")]),
    (f"10.20.30.40 {_CARD_NO}", [("IP", "10.20.30.40"), ("CARD", _CARD_NO)]),
    (f"2001:db8::1 {_CARD_NO} 2", [("IP", "2001:db8::1"), ("CARD", _CARD_NO)]),
    (f"{_CARD_NO} 2001:db8::1", [("CARD", _CARD_NO), ("IP", "2001:db8::1")]),
    (
        "+1 (555) 123-4567 123-45-6789",
        [("PHONE", "+1 (555) 123-4567"), ("NATIONAL_ID", "123-45-6789")],
    ),
]


@pytest.mark.parametrize("mask_ip", [True, False])
@pytest.mark.parametrize(("text", "expected"), _ADJACENT)
def test_rf_adjacent_numeric_entities_are_all_redacted(
    text: str, expected: list[tuple[str, str]], mask_ip: bool
) -> None:
    """RF card next to IPv4/IPv6 and phone next to SSN: each found and redacted, stably."""
    red = _redactor(mask_ip=mask_ip)
    assert [(s.type, text[s.start : s.end]) for s in red.scan(text)] == expected
    result = red.redact(text)
    assert result is not None
    for kind, value in expected:
        assert (value in result.text) is (kind == "IP" and not mask_ip)
    again = red.redact(result.text)
    assert again is not None
    assert again.text == result.text
    blocking = BLOCKING if mask_ip else BLOCKING - {"IP"}
    assert [s.type for s in red.scan(result.text) if s.type in blocking] == []


def test_rf_card_cut_prefers_leftmost_longest_and_respects_word_boundaries() -> None:
    """RF a Luhn-valid 19-digit run stays whole; digits glued to letters are not a card."""
    red = _redactor()
    nineteen = "4111 1111 1111 1110 005"  # Luhn-valid as 19 digits; its 16-digit prefix is not
    assert [(s.type, s.start, s.end) for s in red.scan(nineteen)] == [("CARD", 0, len(nineteen))]
    assert red.scan("x" + _CARD_NO.replace(" ", "") + "y") == []
    text = f"id A{_CARD_NO}, card {_CARD_NO}"  # glued to a letter: only the second is a card
    cards = [s for s in red.scan(text) if s.type == "CARD"]
    assert [text[s.start : s.end] for s in cards] == [_CARD_NO]
    assert cards[0].start == text.rindex(_CARD_NO)


def test_rf_card_cuts_skip_runs_shorter_than_13_digits() -> None:
    """RF digit runs that cannot hold 13 digits are never cut (fast path); text untouched."""
    text = "12:00:05 port 8080 id 1234 5678 9012 req 4111-1111-111"
    assert rp._CARD_RUN.search(text) is None
    assert list(rp._card_cuts(text)) == []
    assert [s.type for s in _redactor().scan(text) if s.type == "CARD"] == []
    assert rp._CARD_RUN.fullmatch("4111 1111 1111 1")  # exactly 13 digits still a run


# --- RF: redact_batch and the failure counter ---------------------------------------------------


def test_rf_redact_batch_fails_closed_per_item() -> None:
    """RF redact_batch keeps order, maps None to None and a failing item to None (+1 failed)."""
    red = _redactor()
    before = r._records_failed_total()
    out = red.redact_batch([None, "mail x@example.com", "a" * 4_000_001, "plain"])
    assert out[0] is None
    assert out[1] is not None
    assert "x@example.com" not in out[1]
    assert out[2] is None
    assert out[3] == "plain"
    assert r._records_failed_total() == before + 1


# --- RF: optional Presidio NER ----------------------------------------------------------------


def test_rf_ner_missing_extra_is_config_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF scan(ner=True) with ner: presidio and no presidio_analyzer -> ConfigError."""
    monkeypatch.setitem(sys.modules, "presidio_analyzer", None)
    red = _redactor(ner="presidio")
    assert red.scan("Bob Smith") == []
    with pytest.raises(ConfigError, match=r"^install the ner extra$"):
        red.scan("Bob Smith", ner=True)


def test_rf_ner_hits_are_added_without_overlap(monkeypatch: pytest.MonkeyPatch) -> None:
    """RF Presidio PERSON hits on the masked text are added; overlapping hits are dropped."""
    seen: list[str] = []

    class _Hit:
        def __init__(self, start: int, end: int) -> None:
            self.start, self.end = start, end

    class _Engine:
        def analyze(self, *, text: str, entities: list[str], language: str) -> list[_Hit]:
            seen.append(text)
            assert entities == ["PERSON"]
            assert language == "en"
            # out-of-range hits (negative start, end past the text) are dropped
            return [_Hit(-4, 3), _Hit(0, 9), _Hit(14, 22), _Hit(27, 32), _Hit(33, 40)]

    fake = types.ModuleType("presidio_analyzer")
    fake.AnalyzerEngine = _Engine  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "presidio_analyzer", fake)
    text = "Bob Smith and Jane Doe met Carol"
    red = _redactor(ner="presidio")
    spans = red.scan(text, ner=True)
    assert [(s.type, text[s.start : s.end]) for s in spans] == [
        ("PERSON", "Bob Smith"),
        ("PERSON", "Jane Doe"),
        ("PERSON", "Carol"),
    ]
    assert "Jane Doe" not in seen[0]
    assert len(seen[0]) == len(text)
    assert _redactor().scan(text, ner=True) == spans[1:2]  # ner: none ignores the flag


# --- PT10-02, PT10-03, PT10-05 properties ---------------------------------------------------------

_PII = (
    "jane.doe@example.com",
    "Jane Doe",
    "Doe, Jane",
    "+1 (555) 123-4567",
    "4111 1111 1111 1111",
    "10.20.30.40",
    "2001:db8::1",
    "123-45-6789",
    "https://h.example/x?sig=abc123&code=zz9",
    f"{KEY_PARAM}={PLANTED}",
    "AKIA" + "ABCDEFGHIJKLMNOP",
    "[PERSON_0123456789]",
    "[SECRET]",
)
_FILLER = st.text(alphabet=st.sampled_from("abcdefXYZ0123456789 .,:;=@-_/?&()[]+\n"), max_size=12)
_PIECES = st.lists(st.one_of(st.sampled_from(_PII), _FILLER), max_size=12)
_SEPS = st.sampled_from([" ", "\n", ", ", "; ", " | "])
_RED = _redactor()


@settings(max_examples=300, deadline=None)
@given(pieces=_PIECES, sep=_SEPS)
def test_pt10_02_redact_is_idempotent(pieces: list[str], sep: str) -> None:
    """PT10-02 redact(redact(x).text).text == redact(x).text for text with injected PII."""
    first = _RED.redact(sep.join(pieces))
    assert first is not None
    second = _RED.redact(first.text)
    assert second is not None
    assert second.text == first.text


@settings(max_examples=300, deadline=None)
@given(pieces=_PIECES, sep=_SEPS)
def test_pt10_03_redacted_text_has_no_blocking_span(pieces: list[str], sep: str) -> None:
    """PT10-03 scan(redact(x).text) reports no span of a blocking type."""
    result = _RED.redact(sep.join(pieces))
    assert result is not None
    assert [s.type for s in _RED.scan(result.text) if s.type in BLOCKING] == []


@given(
    value=st.text(min_size=1, max_size=40),
    kinds=st.lists(st.sampled_from(r.DETECTION_ORDER), min_size=2, max_size=2, unique=True),
)
def test_pt10_05_pseudonym_deterministic_and_type_specific(value: str, kinds: list[Any]) -> None:
    """PT10-05 pseudonym is deterministic, has 10 hex chars, and differs across types."""
    first, second = kinds
    token = _RED.pseudonym(first, value)
    assert token == _RED.pseudonym(first, value)
    assert TOKEN_RE.match(token)
    assert token.startswith(f"[{first}_")
    assert token != _RED.pseudonym(second, value)
