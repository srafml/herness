"""Security tests for the egress guard (impl 10 ST10-10 to ST10-13, ST10-33, ST10-56).

ST10-12's streaming-body half (U10-54) is in test_st10_egress_clients.py (T10-17).
"""

from __future__ import annotations

import json
import multiprocessing
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.egress_harness import (
    API,
    DIRECTORY_NAME,
    audit_fields,
    egress_lines,
    load,
    make_guard,
    race_worker,
    with_egress,
)
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import egress as eg
from herness.core.errors import EgressBlocked
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
MARKER = "ZQX-UNIQUE-7731"
ZWSP, ZWJ, WJ, BOM = chr(0x200B), chr(0x200D), chr(0x2060), chr(0xFEFF)
SHY, MVS, INVISIBLE_TIMES = chr(0x00AD), chr(0x180E), chr(0x2062)
WRITER = """
import sys
from pathlib import Path
from herness.core.egress_log import LINE_KEYS, EgressLog
line = {"egress_id": "egr_other", "decision": "allowed", "tokens_in": int(sys.argv[2])}
EgressLog(Path(sys.argv[1]), "cfg_other", "hybrid").write(dict.fromkeys(LINE_KEYS) | line)
"""


@pytest.fixture(autouse=True)
def _isolate(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


def _full_width(text: str) -> str:
    return "".join(chr(ord(ch) + 0xFEE0) if "!" <= ch <= "~" else ch for ch in text)


def _reason(guard: eg.EgressGuard, body: bytes, **kw: object) -> str | None:
    purpose = kw.pop("purpose", "reasoning_final")
    with pytest.raises(EgressBlocked) as info:
        guard.check(API, body, purpose, "aggregated_evidence", **kw)  # type: ignore[arg-type]
    return info.value.reason


def test_st10_10_json_email_and_directory_name(tmp_path: Path) -> None:
    """ST10-10 JSON with an email and a directory name (escaped as \\u00e9): pii_detected."""
    cfg = load(tmp_path, "hybrid")
    body = json.dumps({"owner": DIRECTORY_NAME, "contact": "john@corp.com"}).encode()
    assert DIRECTORY_NAME.encode() not in body  # only JSON decoding reveals the name
    assert _reason(make_guard(cfg), body) == "pii_detected"
    (line,) = egress_lines(cfg.paths.logs)
    assert line["scan_hits"] == {"EMAIL": 1, "PERSON": 1}


@pytest.mark.parametrize(
    "text",
    [
        _full_width("john@corp.com"),
        f"john{ZWSP}@{ZWJ}corp.com",
        f"card 4111{ZWSP}1111{WJ}1111{BOM}1111",
        f"john{SHY}@corp.com",
        f"john@{INVISIBLE_TIMES}corp{MVS}.com",
    ],
    ids=[
        "full_width_email",
        "zero_width_email",
        "zero_width_card",
        "soft_hyphen_email",
        "invisible_operator_email",
    ],
)
def test_st10_11_normalization_evasions(tmp_path: Path, text: str) -> None:
    """ST10-11 full-width and zero-width evasions are caught after NFKC folding."""
    guard = make_guard(load(tmp_path, "hybrid"))
    assert _reason(guard, text.encode()) == "pii_detected"
    assert _reason(guard, json.dumps({"note": text}).encode()) == "pii_detected"


def test_st10_12_oversized_body(tmp_path: Path) -> None:
    """ST10-12 an oversized body is refused before any scan."""
    cfg = with_egress(load(tmp_path, "hybrid"), max_request_bytes=1_000)
    assert _reason(make_guard(cfg), b"x" * 1_001) == "body_too_large"


def test_st10_12_day_cap_across_two_guards(tmp_path: Path) -> None:
    """ST10-12 two guards with separate in-memory state share the day cap via the log dir."""
    cfg = with_egress(load(tmp_path, "hybrid"), max_tokens_per_day=100)
    first, second = make_guard(cfg), make_guard(cfg)
    first.check(API, b"{}", "reasoning_final", "aggregated_evidence", token_estimate=60)
    assert _reason(second, b"{}", token_estimate=60) == "tokens_per_day"
    second.check(API, b"{}", "reasoning_final", "aggregated_evidence", token_estimate=40)
    assert _reason(first, b"{}", token_estimate=1) == "tokens_per_day"


def test_st10_12_day_cap_across_two_processes(tmp_path: Path) -> None:
    """ST10-12 tokens logged by another process count against this process's day cap."""
    cfg = with_egress(load(tmp_path, "hybrid"), max_tokens_per_day=100)
    guard = make_guard(cfg)
    guard.check(API, b"{}", "reasoning_final", "aggregated_evidence", token_estimate=10)
    subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", WRITER, str(cfg.paths.logs), "85"], cwd=REPO, check=True, timeout=60
    )
    assert _reason(guard, b"{}", token_estimate=10) == "tokens_per_day"
    guard.check(API, b"{}", "reasoning_final", "aggregated_evidence", token_estimate=5)


def test_st10_13_marker_never_reaches_log_files(tmp_path: Path) -> None:
    """ST10-13 a marker in allowed and blocked payloads is absent from every file under logs."""
    cfg = load(tmp_path, "hybrid")
    logs = cfg.paths.logs
    configure_logging("DEBUG", log_dir=logs, scrubber=lambda _l, _m, event: event)
    try:
        guard = make_guard(cfg)
        guard.check(
            API, json.dumps({"note": MARKER}).encode(), "reasoning_final", "aggregated_evidence"
        )
        assert _reason(guard, f"{MARKER} john@corp.com".encode()) == "pii_detected"
        assert _reason(guard, MARKER.encode(), token_estimate=10**7) == "tokens_per_request"
        assert _reason(guard, MARKER.encode(), purpose="reasoning") == "purpose_not_allowed"
    finally:
        reset_logging()
    files = [p for p in logs.rglob("*") if p.is_file()]
    names = {p.name.split("-")[0] for p in files}
    assert {"egress", "audit", "herness"} <= names
    for path in files:
        assert MARKER.encode() not in path.read_bytes(), path.name
    assert [ln["decision"] for ln in egress_lines(logs)] == [
        "allowed",
        "blocked",
        "blocked",
        "blocked",
    ]


def test_st10_33_blocked_call_is_audited_without_payload(tmp_path: Path) -> None:
    """ST10-33 a blocked call writes an audit egress line with egress_id and reason only."""
    cfg = load(tmp_path, "hybrid")
    with pytest.raises(EgressBlocked) as info:
        make_guard(cfg).check(
            API, f"{MARKER} john@corp.com".encode(), "reasoning_final", "aggregated_evidence"
        )
    assert audit_fields(cfg.paths.logs) == [
        {"egress_id": info.value.egress_id, "reason": "pii_detected"}
    ]
    for path in cfg.paths.logs.glob("audit-*.jsonl"):
        text = path.read_text(encoding="utf-8")
        assert MARKER not in text
        assert "john@corp.com" not in text


def _hybrid_dir(tmp_path: Path, *, purposes: str, policy: str | None = None) -> Path:
    cfg_dir = write_full_config(tmp_path)
    hybrid = cfg_dir / "profiles" / "hybrid.yaml"
    hybrid.write_text(hybrid.read_text("utf-8").replace("[reasoning_final]", purposes), "utf-8")
    if policy is not None:
        herness = cfg_dir / "herness.yaml"
        text = herness.read_text("utf-8").replace(
            "hybrid_approved: true, premium_approved: true,", policy
        )
        herness.write_text(text, "utf-8")
    return cfg_dir


def test_st10_56_a_reasoning_purpose_without_chat_approval_fails_c11(tmp_path: Path) -> None:
    """ST10-56 (a) hybrid with reasoning in purposes and chat_approved false: C11 error."""
    cfg_dir = _hybrid_dir(tmp_path, purposes="[reasoning_final, reasoning]")
    issues = c.validate(cfg_dir, "hybrid", offline=True)
    assert [
        (i.message.split(" ")[0], i.severity, i.path) for i in issues if i.severity == "error"
    ] == [("C11", "error", "security.egress.purposes[1]")]


def test_st10_56_b_guard_refuses_unapproved_chat_without_connecting(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST10-56 (b) C11 bypassed: cloud_chat_allowed False; chat_not_approved and no connect."""
    connects: list[object] = []
    monkeypatch.setattr(socket.socket, "connect", lambda _self, addr: connects.append(addr))
    cfg = with_egress(load(tmp_path, "hybrid"), purposes=("reasoning_final", "reasoning"))
    assert cfg.security.data_policy.hybrid_approved is True
    assert cfg.security.data_policy.chat_approved is False
    assert eg.cloud_chat_allowed(cfg) is False
    body = json.dumps({"question": "why did mttr rise", "evidence": [{"metric": "mttr_hours"}]})
    with pytest.raises(EgressBlocked) as info:
        make_guard(cfg).check(API, body.encode(), "reasoning", "aggregated_evidence")
    assert info.value.reason == "chat_not_approved"
    assert connects == []
    assert audit_fields(cfg.paths.logs) == [
        {"egress_id": info.value.egress_id, "reason": "chat_not_approved"}
    ]


def test_st10_56_c_chat_approved_without_hybrid_approval_fails_c25(tmp_path: Path) -> None:
    """ST10-56 (c) chat_approved true without hybrid_approved (or premium): C25 error."""
    cfg_dir = _hybrid_dir(tmp_path, purposes="[reasoning_final]", policy="chat_approved: true,")
    issues = c.validate(cfg_dir, "local", offline=True)
    assert [
        (i.message.split(" ")[0], i.severity, i.path) for i in issues if i.severity == "error"
    ] == [("C25", "error", "security.data_policy.chat_approved")]


def test_st10_12_day_cap_race_between_two_processes(tmp_path: Path) -> None:
    """ST10-12 two processes race the day cap at a barrier: exactly one 60-token call fits 100."""
    cfg = load(tmp_path, "hybrid")
    cfg_dir = tmp_path / "config"
    ctx = multiprocessing.get_context("spawn")
    barrier, results = ctx.Barrier(2), ctx.Queue()
    procs = [
        ctx.Process(target=race_worker, args=(str(cfg_dir), barrier, results)) for _ in range(2)
    ]
    for proc in procs:
        proc.start()
    outcomes = sorted(results.get(timeout=120) for _ in procs)
    for proc in procs:
        proc.join(timeout=60)
        assert proc.exitcode == 0
    assert outcomes == ["allowed", "tokens_per_day"]
    decisions = sorted((ln["decision"], ln["reason"]) for ln in egress_lines(cfg.paths.logs))
    assert decisions == [("allowed", None), ("blocked", "tokens_per_day")]
