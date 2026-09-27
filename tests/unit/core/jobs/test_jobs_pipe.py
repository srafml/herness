"""Tests of U08-84 (pipe messages): UT08-97, ST08-13 and the T08-20 no-pickle acceptance grep."""

from __future__ import annotations

import io
import json
import pickle  # noqa: TID251 - ST08-13 builds a pickle attack payload; nothing here unpickles
import re
import tokenize
from multiprocessing import Pipe
from pathlib import Path

import pytest

from herness.core.errors import SchemaViolation
from herness.core.ids import new_ulid
from herness.core.jobs import pipe
from herness.core.jobs.pipe import (
    MAX_PIPE_MSG_BYTES,
    GpuReplyMsg,
    GpuRequestMsg,
    HeartbeatMsg,
    OutcomeMsg,
    PipeMessage,
    SaveStateMsg,
    StopMsg,
    decode_message,
    encode_message,
)

pytestmark = pytest.mark.unit

JOBS_DIR = Path(pipe.__file__).parent


def _valid_messages() -> list[PipeMessage]:
    rid = new_ulid()
    return [
        StopMsg(reason="preempt"),
        GpuReplyMsg(request_id=rid, ok=True, previous_class="decider", healthy=None),
        GpuReplyMsg(request_id=rid, ok=False, error_class="ConfigError", message="x" * 500),
        HeartbeatMsg(note="n" * 200),
        HeartbeatMsg(),
        SaveStateMsg(state={"a": [1, 2.5, None, {"b": True}], "c": "ü"}),
        GpuRequestMsg(request_id=rid, op="require_class", cls="large", timeout_s=30.0),
        GpuRequestMsg(request_id=rid, op="service_start", service="openjev"),
        OutcomeMsg(status="error", result={}, error_class="ModelUnavailable", error_message="e"),
        OutcomeMsg(status="done", result={"rows": 3}),
    ]


@pytest.mark.parametrize("message", _valid_messages(), ids=lambda m: m.type)
def test_ut08_97_round_trip(message: PipeMessage) -> None:
    """UT08-97 every message type encodes to canonical JSON bytes and decodes back equal."""
    data = encode_message(message)
    assert isinstance(data, bytes)
    assert json.loads(data)["type"] == message.type
    assert decode_message(data) == message


def test_ut08_97_round_trip_over_a_pipe() -> None:
    """UT08-97 bytes survive `send_bytes` / `recv_bytes` on a real pipe pair."""
    parent, child = Pipe(duplex=True)
    try:
        parent.send_bytes(encode_message(StopMsg(reason="cancel")))
        assert decode_message(child.recv_bytes()) == StopMsg(reason="cancel")
    finally:
        parent.close()
        child.close()


def test_ut08_97_encode_rejects_9_mb_message() -> None:
    """UT08-97 a 9 MB message is refused at encode time."""
    big = SaveStateMsg(state={"blob": "x" * 9_000_000})
    with pytest.raises(SchemaViolation, match="exceeds 8 MiB"):
        encode_message(big)


def test_ut08_97_decode_rejects_9_mb_message_before_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UT08-97 an oversized payload is refused before `json.loads` runs."""
    calls: list[object] = []
    monkeypatch.setattr(json, "loads", lambda *a, **k: calls.append(a))
    data = b'{"type":"heartbeat","note":"' + b"x" * 9_000_000 + b'"}'
    with pytest.raises(SchemaViolation, match=r"^bad pipe message$"):
        decode_message(data)
    assert calls == []


def test_ut08_97_max_size_constant() -> None:
    """UT08-97 the pipe cap is exactly 8 388 608 bytes (8 MiB)."""
    assert MAX_PIPE_MSG_BYTES == 8_388_608


def _raw(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


@pytest.mark.parametrize(
    "data",
    [
        _raw({"type": "launch_missiles"}),
        _raw({"type": "heartbeat", "note": None, "extra": 1}),
        _raw({"type": "stop"}),
        _raw({"type": "stop", "reason": "reboot"}),
        _raw({"note": "no type"}),
        _raw({"type": "heartbeat", "note": "x" * 201}),
        _raw({"type": "gpu_reply", "request_id": "not-a-ulid", "ok": True}),
        _raw({"type": "gpu_reply", "request_id": new_ulid(), "ok": "true"}),
        _raw({"type": "gpu_reply", "request_id": new_ulid(), "ok": False, "message": "m" * 501}),
        _raw({"type": "gpu_request", "request_id": new_ulid(), "op": "rm_rf"}),
        _raw({"type": "outcome", "status": "done", "result": {}, "error_message": "e" * 2049}),
        _raw([1, 2]),
        b"not json at all",
        b'{"type":"save_state","state":{"x":NaN}}',
        b"\xff\xfe",
        b"[" * 100_000 + b"]" * 100_000,
    ],
    ids=lambda d: repr(d[:24]),
)
def test_ut08_97_decode_rejects_invalid(data: bytes) -> None:
    """UT08-97 unknown type, extra field, bad values and non-JSON → SchemaViolation."""
    with pytest.raises(SchemaViolation) as info:
        decode_message(data)
    assert info.value.message == "bad pipe message"
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_ut08_97_decode_rejects_non_bytes() -> None:
    """UT08-97 only bytes are decoded."""
    with pytest.raises(SchemaViolation):
        decode_message("{}")  # type: ignore[arg-type]


def test_ut08_97_strict_rejects_coercion() -> None:
    """UT08-97 strict mode: a string number is not coerced into `timeout_s`."""
    data = _raw({"type": "gpu_request", "request_id": new_ulid(), "op": "service_stop",
                 "service": "openjev", "timeout_s": "5"})  # fmt: skip
    with pytest.raises(SchemaViolation):
        decode_message(data)


_TRIGGERED: list[str] = []


class _Evil:
    """Unpickling this object would record a flag (it must never happen)."""

    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (_TRIGGERED.append, ("unpickled",))


def test_st08_13_pickled_bytes_rejected_nothing_unpickled() -> None:
    """ST08-13 pickled bytes from the child → SchemaViolation, and nothing is unpickled."""
    payload = pickle.dumps(_Evil())
    parent, child = Pipe(duplex=True)
    try:
        child.send_bytes(payload)
        received = parent.recv_bytes()
        with pytest.raises(SchemaViolation, match=r"^bad pipe message$"):
            decode_message(received)
    finally:
        parent.close()
        child.close()
    assert _TRIGGERED == []


def test_st08_13_extra_field_rejected() -> None:
    """ST08-13 a JSON message with an extra field → SchemaViolation, no content echoed."""
    data = _raw({"type": "outcome", "status": "done", "result": {}, "secret_extra": "tok-123"})
    with pytest.raises(SchemaViolation) as info:
        decode_message(data)
    assert "tok-123" not in str(info.value)
    assert "tok-123" not in repr(info.value.__dict__)


_FORBIDDEN = re.compile(
    r"\.send\(|\.recv\(|\bimport\s+(pickle|cPickle|marshal|shelve)\b"
    r"|\bfrom\s+(pickle|cPickle|marshal|shelve)\b|\b(pickle|cPickle|marshal|shelve)\."
    r"|\beval\(|\bexec\(|\bmultiprocessing\.Queue\b|\bManager\("
)


def _code_only(source: str) -> list[str]:
    """Source lines with every string literal and comment blanked (no docstring false hits)."""
    lines = source.splitlines()
    tokens = tokenize.generate_tokens(io.StringIO(source).readline)
    for tok in tokens:
        if tok.type not in (tokenize.STRING, tokenize.COMMENT, tokenize.FSTRING_MIDDLE):
            continue
        (r0, c0), (r1, c1) = tok.start, tok.end
        for row in range(r0, r1 + 1):
            text = lines[row - 1]
            lo = c0 if row == r0 else 0
            hi = c1 if row == r1 else len(text)
            lines[row - 1] = text[:lo] + " " * (hi - lo) + text[hi:]
    return lines


def test_cv_t08_20_code_only_blanks_strings_and_comments() -> None:
    """CV-T08-20 the grep helper ignores docstrings and comments but sees code."""
    src = '"""uses pickle.loads"""\nx = 1  # eval(\ny = conn.send(b"")\n'
    hits = [n for n, line in enumerate(_code_only(src), 1) if _FORBIDDEN.search(line)]
    assert hits == [3]
    assert _FORBIDDEN.search("import marshal")
    assert _FORBIDDEN.search("q = multiprocessing.Queue()")
    assert _FORBIDDEN.search("m = mp.Manager()")


def test_cv_t08_20_no_pickle_calls_in_jobs_package() -> None:
    """CV-T08-20 acceptance: no `Connection.send(`/`recv(`, pickle, marshal, eval, exec,
    `multiprocessing.Queue` or `Manager(` anywhere under herness/core/jobs (recursive)."""
    files = sorted(JOBS_DIR.rglob("*.py"))
    assert len(files) > 5
    hits = [
        f"{path.relative_to(JOBS_DIR).as_posix()}:{number}"
        for path in files
        for number, line in enumerate(_code_only(path.read_text("utf-8")), 1)
        if _FORBIDDEN.search(line)
    ]
    assert hits == []
