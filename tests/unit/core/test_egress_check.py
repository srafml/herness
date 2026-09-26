"""Tests for EgressGuard.check, get_guard and cloud_chat_allowed (impl 10 U10-50/51/56/107).

UT10-52's client-config half (``follow_redirects``/``trust_env``) belongs to T10-17 (U10-52).
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.egress_harness import (
    API,
    audit_fields,
    egress_lines,
    fixed_redactor,
    load,
    make_guard,
    with_egress,
)
from tests.support.fake_keyring import MemoryKeyring

from herness.core import audit as a
from herness.core import config as c
from herness.core import egress as eg
from herness.core.egress_log import EgressLog
from herness.core.errors import EgressBlocked, FatalError
from herness.core.redact import RedactionFailed, Redactor

pytestmark = pytest.mark.unit

EVIDENCE = json.dumps({"metric": "mttr_hours", "service": "svc-7", "value": 12.5}).encode()


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


def _blocked(guard: eg.EgressGuard, url: str = API, body: bytes = EVIDENCE, **kw: object) -> str:
    purpose = kw.pop("purpose", "reasoning_final")
    payload = kw.pop("payload_class", "aggregated_evidence")
    with pytest.raises(EgressBlocked) as info:
        guard.check(url, body, purpose, payload, **kw)  # type: ignore[arg-type]
    exc = info.value
    assert exc.egress_id is not None
    prefix = f"egress blocked ({exc.egress_id}): "
    assert str(exc).startswith(prefix)
    reason = str(exc).removeprefix(prefix)
    # U10-108 limits the attribute to ^[a-z_]{1,40}$, so U10-51's ``port_not_443`` is stored
    # as ``invalid`` by herness.core.errors (spec conflict reported for a ruling, T10-16).
    assert exc.reason == (reason if re.fullmatch(r"[a-z_]{1,40}", reason) else "invalid")
    return reason


def test_ut10_48_local_profile_forbids_egress(tmp_path: Path) -> None:
    """UT10-48 profile local: profile_forbids_egress; one blocked line and one audit line."""
    cfg = load(tmp_path, "local")
    calls: list[int] = []

    def factory() -> Redactor:
        calls.append(1)
        return fixed_redactor()

    guard = eg.EgressGuard(cfg, factory, EgressLog(cfg.paths.logs, "cfg_t", "local"))
    assert _blocked(guard) == "profile_forbids_egress"
    (line,) = egress_lines(cfg.paths.logs)
    assert line["decision"] == "blocked"
    assert line["reason"] == "profile_forbids_egress"
    assert line["payload_sha256"] is None
    assert line["scan_hits"] == {}
    assert audit_fields(cfg.paths.logs) == [
        {"egress_id": line["egress_id"], "reason": line["reason"]}
    ]
    assert calls == []  # a profile without egress never needs the redaction key


def test_ut10_48_get_guard_is_cached_and_reset_by_config_reset(tmp_path: Path) -> None:
    """UT10-48 get_guard builds once from get_config; reset_config drops it (U10-10 hook)."""
    load(tmp_path, "local")
    guard = eg.get_guard()
    assert eg.get_guard() is guard
    assert _blocked(guard) == "profile_forbids_egress"
    c.reset_config()
    load(tmp_path / "again", "local")
    assert eg.get_guard() is not guard
    eg.reset_guard()


def test_ut10_49_purpose_and_payload_class(tmp_path: Path) -> None:
    """UT10-49 hybrid: purpose reasoning -> purpose_not_allowed; redacted_text -> class refused."""
    guard = make_guard(load(tmp_path, "hybrid"))
    assert _blocked(guard, purpose="reasoning") == "purpose_not_allowed"
    assert _blocked(guard, payload_class="redacted_text") == "payload_class_not_allowed"
    assert _blocked(guard, payload_class="none") == "payload_class_not_allowed"
    assert _blocked(guard, purpose="model_download", payload_class="none") == "purpose_not_allowed"


def test_ut10_49_constants() -> None:
    """UT10-49 the closed sets and profile tables of U10-50."""
    assert {
        "local": frozenset(),
        "synth": frozenset(),
        "hybrid": frozenset({"aggregated_evidence"}),
        "premium": frozenset({"aggregated_evidence", "redacted_text"}),
    } == eg.PAYLOAD_CLASSES_BY_PROFILE
    assert {"huggingface.co", "cdn-lfs.huggingface.co"} == eg.MODEL_DOWNLOAD_HOSTS
    assert eg.MAX_RESPONSE_BYTES == 52_428_800
    assert eg.CHARS_PER_TOKEN == 3.5
    assert {
        "EMAIL", "PHONE", "CARD", "NATIONAL_ID", "CREDENTIAL", "URL_TOKEN", "EMPLOYEE_ID",
        "USER_ID", "PERSON",
    } == eg.BLOCKING_TYPES  # fmt: skip
    assert {"127.0.0.1", "::1", "localhost"} == eg.LOOPBACK_HOSTS


@pytest.mark.parametrize(
    ("url", "reason"),
    [
        ("http://api.anthropic.com", "scheme_not_https"),
        ("https://api.anthropic.com:8443", "port_not_443"),
        ("https://1.2.3.4", "ip_literal"),
        ("https://u:p@api.anthropic.com", "userinfo_present"),
        ("https://evil.com", "host_not_allowed"),
        ("https://[::1]/v1", "ip_literal"),
        ("https://api.anthropic.com./v1", "host_not_allowed"),
        ("https://api.anthropic.com\x00x", "url_invalid"),
        ("https://exa mple.com", "host_not_allowed"),
    ],
)
def test_ut10_50_url_checks(tmp_path: Path, url: str, reason: str) -> None:
    """UT10-50 hybrid: the five step-3 reason codes (plus IPv6, trailing dot, unparsable)."""
    guard = make_guard(load(tmp_path, "hybrid"))
    assert _blocked(guard, url=url) == reason


def test_ut10_50_allowed_destination_and_port_443(tmp_path: Path) -> None:
    """UT10-50 an explicit :443 and upper-case host pass; the line names host and path only."""
    cfg = load(tmp_path, "hybrid")
    make_guard(cfg).check(
        "https://API.Anthropic.com:443/v1/messages?x=1",
        EVIDENCE,
        "reasoning_final",
        "aggregated_evidence",
    )
    (line,) = egress_lines(cfg.paths.logs)
    assert (line["destination"], line["path"], line["method"]) == (
        "api.anthropic.com",
        "/v1/messages",
        "POST",
    )


def test_ut10_51_body_too_large(tmp_path: Path) -> None:
    """UT10-51 a body of max_request_bytes + 1 is refused with body_too_large."""
    cfg = load(tmp_path, "hybrid")
    body = b"a" * (cfg.security.egress.max_request_bytes + 1)
    assert _blocked(make_guard(cfg), body=body) == "body_too_large"
    make_guard(cfg).check(API, b"a" * 1000, "reasoning_final", "aggregated_evidence")


def test_ut10_52_day_and_request_caps(tmp_path: Path) -> None:
    """UT10-52 2,999,000 today + 2,000 -> tokens_per_day; oversized estimate -> per request."""
    cfg = load(tmp_path, "hybrid")
    guard = make_guard(cfg)
    EgressLog(cfg.paths.logs, "cfg_t", "hybrid").write(
        {"egress_id": "egr_seed", "decision": "allowed", "tokens_in": 2_999_000}
    )
    assert _blocked(guard, token_estimate=2_000) == "tokens_per_day"
    assert _blocked(guard, token_estimate=200_001) == "tokens_per_request"
    guard.check(API, EVIDENCE, "reasoning_final", "aggregated_evidence", token_estimate=1_000)
    lines = egress_lines(cfg.paths.logs)
    assert [(ln["decision"], ln["reason"], ln["tokens_in"]) for ln in lines[1:]] == [
        ("blocked", "tokens_per_day", 2_000),
        ("blocked", "tokens_per_request", 200_001),
        ("allowed", None, 1_000),
    ]


def test_ut10_52_estimate_from_body_length(tmp_path: Path) -> None:
    """UT10-52 without an estimate tokens = ceil(len(body) / 3.5); the ticket carries it."""
    cfg = load(tmp_path, "hybrid")
    ticket = make_guard(cfg)._check_and_log(
        API,
        b"x" * 8,
        "reasoning_final",
        "aggregated_evidence",
        None,
        method="PUT",
        run_id="run_1",
        task_id="task_1",
    )
    assert ticket.egress_id.startswith("egr_")
    assert ticket.tokens_in == 3
    (line,) = egress_lines(cfg.paths.logs)
    assert (line["tokens_in"], line["method"], line["run_id"], line["task_id"]) == (
        3,
        "PUT",
        "run_1",
        "task_1",
    )
    assert line["egress_id"] == ticket.egress_id


def test_ut10_53_email_is_blocked_with_counts(tmp_path: Path) -> None:
    """UT10-53 a raw email: pii_detected with hit counts only."""
    cfg = load(tmp_path, "hybrid")
    body = b"summary: contact john@corp.com or mary@corp.com"
    assert _blocked(make_guard(cfg), body=body) == "pii_detected"
    (line,) = egress_lines(cfg.paths.logs)
    assert line["scan_hits"] == {"EMAIL": 2}


def test_ut10_53_credential_is_blocked(tmp_path: Path) -> None:
    """UT10-53 a ``password=...`` assignment is blocked."""
    guard = make_guard(load(tmp_path, "hybrid"))
    body = ("pass" + "word=" + "Tr0ub4dor-x9").encode()
    assert _blocked(guard, body=body) == "pii_detected"


def test_ut10_53_only_pseudonym_tokens_are_allowed(tmp_path: Path) -> None:
    """UT10-53 a body of pseudonym tokens only is allowed: hash logged, no hits."""
    cfg = load(tmp_path, "hybrid")
    red = fixed_redactor()
    tokens = [red.pseudonym("EMAIL", "john@corp.com"), red.pseudonym("PERSON", "José García")]
    body = json.dumps({"owner": tokens[1], "contacts": [tokens[0], "[SECRET]"]}).encode()
    make_guard(cfg, red).check(API, body, "reasoning_final", "aggregated_evidence")
    (line,) = egress_lines(cfg.paths.logs)
    assert line["decision"] == "allowed"
    assert line["scan_hits"] == {}
    assert line["payload_sha256"] == hashlib.sha256(body).hexdigest()
    assert line["bytes_out"] == len(body)


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        (b"\xff\xfe", "body_not_utf8"),
        (b"a" * 1_000_001, "string_too_long"),
        (json.dumps(["a" * 1_000_001]).encode(), "string_too_long"),
    ],
    ids=["not_utf8", "text_too_long", "json_string_too_long"],
)
def test_ut10_53_undecodable_and_overlong(tmp_path: Path, body: bytes, reason: str) -> None:
    """UT10-53 non-UTF-8 bodies and strings over 1,000,000 characters are refused."""
    cfg = with_egress(load(tmp_path, "hybrid"), max_tokens_per_request=2_000_000)
    assert _blocked(make_guard(cfg), body=body) == reason


def test_ut10_53_json_keys_duplicates_and_scalars(tmp_path: Path) -> None:
    """UT10-53 object keys, values hidden by a duplicate key and bare JSON scalars are scanned."""
    guard = make_guard(load(tmp_path, "hybrid"))
    assert _blocked(guard, body=b'{"john@corp.com": 1}') == "pii_detected"
    assert _blocked(guard, body=b'{"a": "john@corp.com", "a": "x"}') == "pii_detected"
    assert _blocked(guard, body=b"4111111111111111") == "pii_detected"
    guard.check(API, b'[[{"n": null, "ok": true}], 3]', "reasoning_final", "aggregated_evidence")


def test_ut10_53_ip_blocks_only_with_mask_ip(tmp_path: Path) -> None:
    """UT10-53 an IP address blocks when mask_ip is on (default) and passes when it is off."""
    cfg = load(tmp_path, "hybrid")
    body = b"host 10.20.30.40 was slow"
    assert _blocked(make_guard(cfg), body=body) == "pii_detected"
    red = cfg.security.redaction.model_copy(update={"mask_ip": False})
    off = cfg.model_copy(update={"security": cfg.security.model_copy(update={"redaction": red})})
    make_guard(off).check(API, body, "reasoning_final", "aggregated_evidence")


def test_ut10_53_scan_failure_fails_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-53 a detector failure is a refusal (scan_failed), never an allowed call."""
    red = fixed_redactor()

    def boom(_self: Redactor, _text: str, *, ner: bool = False) -> list[object]:
        msg = "EMAIL"
        raise RedactionFailed(msg)

    monkeypatch.setattr(Redactor, "scan", boom)
    assert _blocked(make_guard(load(tmp_path, "hybrid"), red)) == "scan_failed"


def test_ut10_53_audit_failure_is_egress_log_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-53 when the audit line cannot be written the refusal reads egress_log_failed."""
    guard = make_guard(load(tmp_path, "local"))

    def fail(*_args: object, **_kw: object) -> None:
        msg = "audit write failed: egress"
        raise FatalError(msg)

    monkeypatch.setattr(a, "audit", fail)
    assert _blocked(guard) == "egress_log_failed"


def test_ut10_53_model_download_window_closed_until_t10_17(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-53 without a U10-55 window model_download is refused; inside one, V-17 hosts pass."""
    cfg = load(tmp_path, "hybrid")
    guard = make_guard(cfg)
    url = "https://huggingface.co/Qwen/x/resolve/main/model.safetensors"
    assert (
        _blocked(guard, url=url, purpose="model_download", payload_class="none")
        == "purpose_not_allowed"
    )
    monkeypatch.setattr(eg.EgressGuard, "_window_open", lambda _self: True)
    guard.check(url, b"", "model_download", "none")
    assert (
        _blocked(guard, url="https://evil.com/x", purpose="model_download", payload_class="none")
        == "host_not_allowed"
    )


def test_ut10_79_cloud_chat_allowed(tmp_path: Path) -> None:
    """UT10-79 local, unapproved hybrid False; approved hybrid with reasoning, premium True."""
    assert eg.cloud_chat_allowed(load(tmp_path / "l", "local")) is False
    c.reset_config()
    hybrid = load(tmp_path / "h", "hybrid")
    assert eg.cloud_chat_allowed(hybrid) is False
    assert (
        eg.cloud_chat_allowed(with_egress(hybrid, purposes=("reasoning_final", "reasoning")))
        is False
    )
    c.reset_config()
    approved = load(
        tmp_path / "a", "hybrid", chat_approved=True, purposes=("reasoning_final", "reasoning")
    )
    assert eg.cloud_chat_allowed(approved) is True
    assert eg.cloud_chat_allowed(with_egress(approved, enabled=False)) is False
    c.reset_config()
    premium = load(tmp_path / "p", "premium")
    assert eg.cloud_chat_allowed(premium) is True
    assert eg.cloud_chat_allowed(with_egress(premium, purposes=("reasoning_final",))) is False


def test_ut10_79_guard_refuses_unapproved_chat(tmp_path: Path) -> None:
    """UT10-79 hybrid without chat_approved: purpose reasoning -> chat_not_approved at the guard."""
    cfg = with_egress(load(tmp_path, "hybrid"), purposes=("reasoning_final", "reasoning"))
    assert _blocked(make_guard(cfg), purpose="reasoning") == "chat_not_approved"
    c.reset_config()
    approved = load(
        tmp_path / "a", "hybrid", chat_approved=True, purposes=("reasoning_final", "reasoning")
    )
    make_guard(approved).check(API, EVIDENCE, "reasoning", "aggregated_evidence")
