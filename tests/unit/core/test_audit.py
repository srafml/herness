"""Tests for the hash-chained audit log (impl 10 U10-60 to U10-64, T10-05)."""

from __future__ import annotations

import datetime
import json
import os
import sys
import tempfile
import types
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given, settings
from hypothesis import strategies as st
from tests.support.config_tree import write_full_config

from herness.core import audit as a
from herness.core import config as c
from herness.core.errors import ConfigError, FatalError, SchemaViolation, StoreBusy
from herness.core.ids import canonical_json, sha256_hex

pytestmark = pytest.mark.unit

UTC = datetime.UTC
DAY1 = datetime.datetime(2026, 9, 23, 10, 0, tzinfo=UTC)
DAY2 = datetime.datetime(2026, 9, 24, 9, 0, tzinfo=UTC)
USER = "0123456789abcdef0123456789abcdef"
CHANGE: dict[str, Any] = {"old_hash": None, "new_hash": "h", "profile": "local"}
KNOWN_SECRET = "s3cr3t-Known-Value-77"  # noqa: S105 - test sentinel


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()  # local reset until T11-40 wires reset_config into tests/conftest.py


@pytest.fixture
def cfg(tmp_path: Path) -> c.HernessConfig:
    return c.init_config(config_dir=write_full_config(tmp_path), env={})


class _Clock:
    """Settable wall clock for ``clock.now``."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, start: datetime.datetime) -> None:
        self.now = start
        monkeypatch.setattr(a.clock, "now", lambda: self.now)

    def set(self, value: datetime.datetime) -> None:
        self.now = value


def _lines(path: Path) -> list[bytes]:
    return path.read_bytes().splitlines()


def _review(n: int = 0) -> dict[str, Any]:
    return {
        "item_id": f"f-{n}",
        "kind": "finding",
        "status": "accepted",
        "decided_by": USER,
        "note_len": n,
    }


# --- U10-60 / U10-61 -------------------------------------------------------------------------


def test_ut10_57_chain_across_two_dates(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-57 first prev_hash is 64 zeros; next-day first line chains; canonical JSON."""
    clk = _Clock(monkeypatch, DAY1)
    a.audit("review_decision", USER, **_review(1))
    a.audit("admin_action", "system", action="backup", target="data", counts="files=3")
    day1 = cfg.paths.logs / "audit-2026-09-23.jsonl"
    first, second = _lines(day1)
    assert json.loads(first)["prev_hash"] == "0" * 64
    assert json.loads(second)["prev_hash"] == sha256_hex(first)
    clk.set(DAY2)
    a.audit("auth", USER, user_ref=USER, role="admin", result="ok")
    (third,) = _lines(cfg.paths.logs / "audit-2026-09-24.jsonl")
    record = json.loads(third)
    assert record["prev_hash"] == sha256_hex(second)
    assert third.decode("utf-8") == canonical_json(record)
    assert set(record) == {"ts", "audit_id", "event", "actor", "fields", "config_hash", "prev_hash"}
    assert record["audit_id"].startswith("aud_")
    assert record["ts"] == "2026-09-24T09:00:00.000000Z"
    assert record["config_hash"] == c.config_hash(cfg)
    assert record["fields"] == {"user_ref": USER, "role": "admin", "result": "ok"}
    assert a.verify_chain(cfg.paths.logs) == a.ChainReport(True, 2, 3, None)


def test_ut10_57_append_without_chain_and_lock_held(tmp_path: Path) -> None:
    """UT10-57 plain mode passes None to the builder; lock_held skips re-locking."""
    seen: list[bytes | None] = []

    def build(prev: bytes | None) -> bytes:
        seen.append(prev)
        return b'{"n":1}\n'

    target, lock = tmp_path / "x.jsonl", tmp_path / ".x.lock"
    a.append_jsonl_locked(target, build, lock_path=lock)
    with a.log_lock(lock):
        a.append_jsonl_locked(target, build, lock_path=lock, lock_held=True)
    assert seen == [None, None]
    assert target.read_bytes() == b'{"n":1}\n{"n":1}\n'


def test_ut10_57_previous_line_lookup(tmp_path: Path) -> None:
    """UT10-57 chain mode reads the last terminated line, skipping empty files."""
    (tmp_path / "audit-2026-09-01.jsonl").write_bytes(b"old-1\n" + b"x" * 9000 + b"\n")
    (tmp_path / "audit-2026-09-02.jsonl").write_bytes(b"")
    seen: list[bytes | None] = []

    def build(prev: bytes | None) -> bytes:
        seen.append(prev)
        return b"new\n"

    today = tmp_path / "audit-2026-09-03.jsonl"
    kw: dict[str, Any] = {"lock_path": tmp_path / ".l", "chain_glob": "audit-*.jsonl"}
    a.append_jsonl_locked(today, build, **kw)
    a.append_jsonl_locked(today, build, **kw)
    assert seen == [b"x" * 9000, b"new"]
    single = tmp_path / "single.jsonl"
    single.write_bytes(b"only\npartial")
    assert a._last_line(single) == b"only"
    single.write_bytes(b"no newline")
    assert a._last_line(single) is None


def test_ut10_57_write_error_and_timeout_are_store_busy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-57 an OSError while appending, or a lock timeout, becomes StoreBusy."""
    missing = tmp_path / "missing" / "f.jsonl"
    with pytest.raises(StoreBusy, match=r"^log write failed: f\.jsonl$"):
        a.append_jsonl_locked(missing, lambda _p: b"x\n", lock_path=tmp_path / ".l")
    with (
        pytest.raises(StoreBusy, match=r"^log write failed: \.l$"),
        a.log_lock(missing.parent / ".l"),
    ):
        pass

    def held(_fd: int, *, unlock: bool = False) -> None:
        raise OSError

    monkeypatch.setattr(a, "_os_lock", held)
    now = [0.0]
    monkeypatch.setattr(a, "_monotonic", lambda: now[0])
    monkeypatch.setattr(a, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    with pytest.raises(StoreBusy, match=r"^log lock timeout: \.l$"):  # noqa: SIM117 - raises inner
        with a.log_lock(tmp_path / ".l", timeout_s=1.0):
            pass
    assert now[0] >= 1.0


def test_ut10_57_thread_lock_timeout(tmp_path: Path) -> None:
    """UT10-57 the per-process thread lock also bounds the wait."""
    lock = tmp_path / ".l"
    with a.log_lock(lock), pytest.raises(StoreBusy, match=r"^log lock timeout: \.l$"):
        a.log_lock(lock, timeout_s=0.01).__enter__()


def test_ut10_58_invalid_input_writes_nothing(cfg: c.HernessConfig) -> None:
    """UT10-58 unknown key, missing key, invalid actor, bad values: SchemaViolation, no file."""
    bad: list[tuple[str, str, dict[str, Any]]] = [
        ("review_decision", USER, {**_review(), "extra": "x"}),
        ("recommendation_decision", USER, {"rec_id": "r1", "decision": "ok"}),
        ("auth", "Alice", {"user_ref": USER, "role": "admin", "result": "ok"}),
        ("auth", USER.upper(), {"user_ref": USER, "role": "admin", "result": "ok"}),
        ("egress", USER, {"egress_id": "e", "reason": 1.5}),
        ("egress", USER, {"egress_id": "e", "reason": "x" * 257}),
        ("config_change", USER, {**CHANGE, "changed_paths": ["p"] * 201}),
        ("config_change", USER, {**CHANGE, "changed_paths": [1]}),
        ("admin_action", USER, {"action": "format_disk", "target": "c"}),
        ("admin_action", USER, {"action": "purge", "target": "lake", "counts": "a=1;;"}),
        ("admin_action", USER, {"action": "purge", "target": "lake", "detail": "y" * 300}),
        ("login", USER, {}),
    ]
    for event, actor, fields in bad:
        with pytest.raises(SchemaViolation):
            a.audit(event, actor, **fields)  # type: ignore[arg-type]
    assert not list(cfg.paths.logs.glob("audit-*.jsonl"))


def test_ut10_58_known_secret_and_credential_refused(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-58 a value containing a known secret or a credential: SchemaViolation, no file."""
    monkeypatch.setattr(a, "_known_values", lambda: frozenset({"", KNOWN_SECRET}))
    cases: list[dict[str, Any]] = [
        {"action": "secret_set", "target": f"x{KNOWN_SECRET}y"},
        {"action": "purge", "target": "t", "detail": "password=hunter2"},
    ]
    for fields in cases:
        with pytest.raises(SchemaViolation, match="audit field would contain a secret") as exc:
            a.audit("admin_action", USER, **fields)
        assert KNOWN_SECRET not in str(exc.value)
        assert "hunter2" not in str(exc.value)
    changed = {
        "old_hash": None,
        "new_hash": "h",
        "profile": "local",
        "changed_paths": ["a.b", KNOWN_SECRET],
    }
    with pytest.raises(SchemaViolation):
        a.audit("config_change", "system", **changed)
    assert not list(cfg.paths.logs.glob("audit-*.jsonl"))
    a.audit("admin_action", "eval", action="purge", target="t", detail=None, counts="a=1;b=2")
    assert len(list(cfg.paths.logs.glob("audit-*.jsonl"))) == 1


def test_ut10_58_known_values_import_rules(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-58 _known_values uses secrets.known_values, empty only while secrets is absent."""
    monkeypatch.delitem(sys.modules, "herness.core.secrets", raising=False)
    fake = types.ModuleType("herness.core.secrets")
    fake.known_values = lambda: frozenset({"abc"})  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "herness.core.secrets", fake)
    assert a._known_values() == frozenset({"abc"})
    monkeypatch.setitem(sys.modules, "herness.core.secrets", None)  # import -> ModuleNotFound
    assert a._known_values() == frozenset()

    def broken(name: str) -> object:
        msg = "No module named 'keyring'"
        raise ModuleNotFoundError(msg, name="keyring")

    monkeypatch.setattr(a.importlib, "import_module", broken)
    with pytest.raises(ModuleNotFoundError):
        a._known_values()


def test_ut10_58_store_busy_becomes_fatal(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-58 StoreBusy from the append becomes FatalError naming the event only."""

    def busy(*_a: object, **_k: object) -> None:
        msg = "log lock timeout: .audit.lock"
        raise StoreBusy(msg)

    monkeypatch.setattr(a, "append_jsonl_locked", busy)
    with pytest.raises(FatalError, match=r"^audit write failed: egress$"):
        a.audit("egress", "system", egress_id="egr_1", reason="blocked")


def test_ut10_58_config_hash_null_on_config_error(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-58 config_hash is memoised per config object and null when hashing fails."""
    calls: list[int] = []

    def failing(_cfg: object) -> str:
        calls.append(1)
        msg = "no key"
        raise ConfigError(msg)

    monkeypatch.setattr(a, "config_hash", failing)
    monkeypatch.setattr(a, "_HASH_MEMO", [])
    a.audit("egress", "system", egress_id="egr_1", reason="r")
    a.audit("egress", "system", egress_id="egr_2", reason="r")
    lines = _lines(next(cfg.paths.logs.glob("audit-*.jsonl")))
    assert [json.loads(line)["config_hash"] for line in lines] == [None, None]
    assert calls == [1]


# --- U10-62 verify_chain ---------------------------------------------------------------------


def _chain(cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Three lines on DAY1 and two on DAY2; returns the two files."""
    clk = _Clock(monkeypatch, DAY1)
    for n in range(3):
        clk.set(DAY1 + datetime.timedelta(seconds=n))
        a.audit("review_decision", USER, **_review(n))
    for n in range(3, 5):
        clk.set(DAY2 + datetime.timedelta(seconds=n))
        a.audit("review_decision", USER, **_review(n))
    monkeypatch.setattr(a.clock, "now", lambda: datetime.datetime.now(UTC))  # real mtime ages
    monkeypatch.setattr(a.clock, "now", lambda: datetime.datetime.now(UTC))  # real mtime ages
    logs = cfg.paths.logs
    return logs / "audit-2026-09-23.jsonl", logs / "audit-2026-09-24.jsonl"


def _write(path: Path, lines: list[bytes], *, old: bool = True, tail: bytes = b"\n") -> None:
    path.write_bytes(b"\n".join(lines) + tail)
    if old:
        os.utime(path, (1_700_000_000, 1_700_000_000))


def test_ut10_59_valid_and_tampered_chains(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-59 valid chain ok; edited, deleted, swapped, truncated: break at file:line."""
    f1, f2 = _chain(cfg, monkeypatch)
    logs = cfg.paths.logs
    assert a.verify_chain(logs) == a.ChainReport(True, 2, 5, None)
    orig1, orig2 = _lines(f1), _lines(f2)
    edited = orig1[1].replace(b'"f-1"', b'"f-9"')
    cases = [
        (f1, [orig1[0], edited, orig1[2]], "audit-2026-09-23.jsonl:3"),
        (f1, [orig1[0], orig1[2]], "audit-2026-09-23.jsonl:2"),
        (f1, [orig1[0], orig1[2], orig1[1]], "audit-2026-09-23.jsonl:2"),
        (f1, orig1[:2], "audit-2026-09-24.jsonl:1"),
    ]
    for path, lines, where in cases:
        _write(path, lines)
        report = a.verify_chain(logs)
        assert (report.ok, report.first_break) == (False, where)
        _write(f1, orig1)
    _write(f2, [orig2[0], orig2[1][:-10]], tail=b"")
    assert a.verify_chain(logs).first_break == "audit-2026-09-24.jsonl:2"
    _write(f2, [orig2[0], orig2[1][:-10]], tail=b"", old=False)  # write in progress
    assert a.verify_chain(logs) == a.ChainReport(True, 2, 4, None)


def test_ut10_59_malformed_lines_and_unreadable_file(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-59 bad JSON, keys, audit_id or ts order, and unreadable files are breaks."""
    f1, _f2 = _chain(cfg, monkeypatch)
    logs = cfg.paths.logs
    orig = _lines(f1)
    rec = json.loads(orig[0])

    def variant(**change: Any) -> bytes:
        return canonical_json({**rec, **change}).encode("utf-8")

    for first in (
        b"not json",
        b"[1]",
        variant(audit_id="x_1"),
        variant(ts="bad"),
        canonical_json({"ts": rec["ts"]}).encode(),
    ):
        _write(f1, [first, *orig[1:]])
        assert a.verify_chain(logs).first_break == "audit-2026-09-23.jsonl:1"
    _write(f1, [variant(ts="2026-09-30T00:00:00.000000Z"), *orig[1:]])
    assert a.verify_chain(logs).first_break == "audit-2026-09-23.jsonl:2"
    _write(f1, orig)
    (logs / "audit-2026-09-25.jsonl").mkdir()
    assert a.verify_chain(logs).first_break == "audit-2026-09-25.jsonl:0"
    assert a.verify_chain(logs / "none") == a.ChainReport(True, 0, 0, None)


def test_pt10_08_single_byte_mutation_detected(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """PT10-08 any single-byte mutation of any chained line is detected."""
    files = [(p.name, _lines(p)) for p in _chain(cfg, monkeypatch)]
    # Every line except the newest one has a successor whose prev_hash covers its bytes.
    targets = [(fi, li) for fi, (_n, lines) in enumerate(files) for li in range(len(lines))]
    targets.pop()

    @settings(max_examples=150, deadline=None)
    @given(st.sampled_from(targets), st.integers(min_value=0), st.integers(1, 255))
    def check(target: tuple[int, int], pos: int, delta: int) -> None:
        fi, li = target
        with tempfile.TemporaryDirectory() as tmp:
            for index, (name, lines) in enumerate(files):
                copy = list(lines)
                if index == fi:
                    line = bytearray(copy[li])
                    at = pos % len(line)
                    line[at] = (line[at] + delta) % 256
                    copy[li] = bytes(line)
                _write(Path(tmp) / name, copy)
            assert not a.verify_chain(Path(tmp)).ok

    check()


# --- U10-63 record_config_change -------------------------------------------------------------


def test_ut10_21_record_config_change(cfg: c.HernessConfig, tmp_path: Path) -> None:
    """UT10-21 same hash twice then a change: one config_change line, snapshot, LAST."""
    h1 = a.record_config_change(cfg)
    assert a.record_config_change(cfg) == h1
    changed = c.load_config(
        config_dir=tmp_path / "config", env={}, overrides=["retention.traces_days=30"]
    )
    h2 = a.record_config_change(changed, actor=USER)
    assert h2 != h1
    lines = [json.loads(x) for x in _lines(next(cfg.paths.logs.glob("audit-*.jsonl")))]
    assert [x["fields"]["new_hash"] for x in lines] == [h1, h2]
    assert lines[0]["fields"]["old_hash"] is None
    assert lines[0]["fields"]["changed_paths"] == []
    last = lines[1]
    assert (last["event"], last["actor"], last["config_hash"]) == ("config_change", USER, h2)
    assert last["fields"]["old_hash"] == h1
    assert last["fields"]["changed_paths"] == ["retention.traces_days"]
    assert last["fields"]["profile"] == "local"
    snaps = cfg.paths.data / "config_snapshots"
    assert (snaps / "LAST").read_text("utf-8") == h2
    snapshot = yaml.safe_load((snaps / f"{h2}.yaml").read_text("utf-8"))
    assert snapshot["retention"]["traces_days"] == 30
    (snaps / "LAST").unlink()  # falls back to the newest config_change line
    assert a.record_config_change(changed) == h2
    assert len(_lines(next(cfg.paths.logs.glob("audit-*.jsonl")))) == 2


def test_ut10_21_changed_paths_are_capped() -> None:
    """UT10-21 changed_paths is sorted and capped with a final "+<n> more" entry."""
    old = {"a": {f"k{i:03d}": i for i in range(250)}, "same": [1], "gone": 1}
    new = {"a": {f"k{i:03d}": -i - 1 for i in range(250)}, "same": [1], "b": {"c": 1}}
    paths = a._changed_paths(old, new)
    assert len(paths) == 200
    assert paths[0] == "a.k000"
    assert paths[-1] == "+53 more"
    assert a._changed_paths({"x": 1}, {"x": 2}) == ["x"]


# --- U10-64 last_secret_set_times ------------------------------------------------------------


def test_ut10_72_last_secret_set_times(
    cfg: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-72 newest secret_set or secret_rotate ts per target; None when never set."""
    clk = _Clock(monkeypatch, DAY1)
    a.audit("admin_action", USER, action="secret_set", target="b")
    a.audit("admin_action", USER, action="secret_set", target="a")
    clk.set(DAY2)
    a.audit("admin_action", USER, action="secret_rotate", target="a")
    a.audit("admin_action", USER, action="backup", target="a")
    a.audit("egress", "system", egress_id="egr_1", reason="r")
    logs = cfg.paths.logs
    with (logs / "audit-2026-09-24.jsonl").open("ab") as fh:
        fh.write(b"garbage\n")
    got = a.last_secret_set_times(logs, ["a", "b", "c"])
    assert got == {"a": DAY2, "b": DAY1, "c": None}
    assert a.last_secret_set_times(logs, ["a"]) == {"a": DAY2}
    assert a.last_secret_set_times(logs / "none", ["a"]) == {"a": None}
