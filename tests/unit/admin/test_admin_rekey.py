"""Tests for the redaction rekey job (impl 10 U10-101 to U10-103; UT10-70).

Key values never reach logs, audit lines, saved state, the marker or the map; only the 8-hex
key ids do. Label rewrites are staged and verified, then swapped in after the key swap.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
import structlog
from tests.support.fake_keyring import MemoryKeyring
from tests.support.rekey_env import (
    AMBIGUOUS_IDS,
    GONE_OLD,
    NEW_ID,
    NEW_KEY,
    OLD_ID,
    OLD_KEY,
    QSV,
    SHARED_OLD,
    FakeCtx,
    RekeyEnv,
    open_rekey_env,
    redactor,
    write_labels,
)

from herness.admin import rekey
from herness.admin.maintenance import handle_maintenance
from herness.admin.rekey import build_rekey_map, rekey_labels, run_rekey
from herness.core import redact
from herness.core import time as clock
from herness.core.errors import FatalError, SchemaViolation, StoreBusy
from herness.core.jobs.ports import JobContext
from herness.core.redact import RedactionFailed, RedactionResult, Redactor
from herness.core.types import JobOutcome

pytestmark = pytest.mark.unit

NOW = datetime.datetime(2026, 9, 26, 19, 0, tzinfo=datetime.UTC)


@pytest.fixture
def rekey_env(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[RekeyEnv]:
    """The planted rekey environment of ``tests.support.rekey_env``."""
    with open_rekey_env(tmp_path, fake_keyring) as env:
        yield env


def _ctx(ctx: FakeCtx) -> JobContext:
    return cast("JobContext", ctx)


def _plant_labels(env: RekeyEnv) -> None:
    write_labels(env, "human", "a", [("sn:incident:INC1", env.old["sn:incident:INC1"])])
    write_labels(env, "gold", "a", [("sn:incident:INC2", SHARED_OLD), ("x", GONE_OLD)])
    write_labels(env, "teacher", "a", [("sn:change:CHG1", env.old["sn:change:CHG1"])])


# --- UT10-70 run_rekey ---------------------------------------------------------------------


def test_ut10_70_run_rekey_end_to_end(rekey_env: RekeyEnv) -> None:
    """UT10-70 map written; labels re-keyed; ambiguous=1; key swapped; marker written; audit."""
    env = rekey_env
    _plant_labels(env)
    ctx = FakeCtx()
    with structlog.testing.capture_logs() as logs:
        outcome = run_rekey(env.cfg, _ctx(ctx), now=NOW)
    assert outcome.status == "done"
    assert outcome.result["counts"] == {"rekeyed": 2, "kept_old": 1, "ambiguous": 1}
    assert (outcome.result["new_key_id"], outcome.result["old_key_id"]) == (NEW_ID, OLD_ID)
    rows = pq.read_table(env.map_path).to_pylist()
    assert {r["record_id"]: (r["old_content_hash"], r["new_content_hash"]) for r in rows} == {
        rid: (env.old[rid], env.new[rid]) for rid in env.old
    }
    assert pq.read_schema(env.map_path).names == [
        "record_id",
        "old_content_hash",
        "new_content_hash",
    ]
    assert [r["content_hash"] for r in env.label_rows("human")] == [env.new["sn:incident:INC1"]]
    gold = sorted((r["record_id"], r["content_hash"]) for r in env.label_rows("gold"))
    assert gold == sorted(
        [("sn:incident:INC2", env.new[rid]) for rid in AMBIGUOUS_IDS] + [("x", GONE_OLD)]
    )
    assert [r["content_hash"] for r in env.label_rows("teacher")] == [env.new["sn:change:CHG1"]]
    assert not [p for p in env.labels.rglob("*") if p.name.startswith(".")]  # nothing staged left
    assert (env.key("redact.hmac_key"), env.key("redact.hmac_key.next")) == (NEW_KEY, None)
    marker = json.loads(env.marker.read_text("utf-8"))
    assert marker == {
        "swapped_at": clock.format_utc(NOW),
        "old_key_id": OLD_ID,
        "new_key_id": NEW_ID,
    }
    lines = [ln["fields"] for ln in env.audit_lines()]
    want = {"action": "redact_rekey", "target": NEW_ID, "detail": f"old={OLD_ID}"}
    assert want in lines
    assert [s["step"] for s in ctx.saves] == [2, 3]
    assert ctx.beats >= 1
    leaks = [env.audit_text(), repr(logs), json.dumps(ctx.saves), env.marker.read_text("utf-8")]
    leaks.append(json.dumps(outcome.result))
    leaks.append(json.dumps(pq.read_table(env.map_path).to_pylist()))
    assert not any(key in text for key in (OLD_KEY, NEW_KEY) for text in leaks)


def test_ut10_70_redactor_is_reset_after_swap(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-70 step 4e resets the process redactor so the next use loads the new key."""
    env = rekey_env
    calls: list[str] = []
    monkeypatch.setattr(rekey, "reset_redactor", lambda: calls.append("reset"))
    run_rekey(env.cfg, _ctx(FakeCtx()), now=NOW)
    assert calls == ["reset"]
    redact.reset_redactor()
    assert redact.get_redactor().key_id == NEW_ID


def test_ut10_70_no_staged_key_is_fatal(rekey_env: RekeyEnv) -> None:
    """UT10-70 precondition: no ``.next`` and no begun swap -> FatalError("no staged key")."""
    env = rekey_env
    del env.keyring.store[("herness", "redact.hmac_key.next")]
    with pytest.raises(FatalError, match="no staged key"):
        run_rekey(env.cfg, _ctx(FakeCtx()), now=NOW)
    assert env.key("redact.hmac_key") == OLD_KEY
    assert not env.map_path.exists()


def test_ut10_70_staged_key_equal_to_active_is_fatal(rekey_env: RekeyEnv) -> None:
    """UT10-70 a staged key equal to the active key is refused; nothing changes."""
    env = rekey_env
    env.keyring.store[("herness", "redact.hmac_key.next")] = OLD_KEY
    with pytest.raises(FatalError, match="staged key equals the active key"):
        run_rekey(env.cfg, _ctx(FakeCtx()), now=NOW)
    assert not env.map_path.exists()


def test_ut10_70_malformed_key_names_only_the_secret(rekey_env: RekeyEnv) -> None:
    """UT10-70 a staged value that is not 64 hex fails; the message holds the name only."""
    env = rekey_env
    bad = "not-hex-" * 8
    env.keyring.store[("herness", "redact.hmac_key.next")] = bad
    with pytest.raises(FatalError) as caught:
        run_rekey(env.cfg, _ctx(FakeCtx()), now=NOW)
    assert "redact.hmac_key.next" in str(caught.value)
    assert bad not in str(caught.value)


def test_ut10_70_resume_after_swap_goes_to_4b(rekey_env: RekeyEnv) -> None:
    """UT10-70 active id equals the saved new_key_id: no rebuild; staged labels swapped in,
    ``.next`` deleted, marker and audit written."""
    env = rekey_env
    _plant_labels(env)
    first = FakeCtx()
    rekey.build_rekey_map(
        env.cfg, redactor(env.cfg, NEW_KEY), out_path=env.map_path, ctx=_ctx(first)
    )
    counts = rekey_labels(env.labels, env.map_path)
    env.keyring.store[("herness", "redact.hmac_key")] = NEW_KEY  # 4a happened, then a kill
    env.map_path.unlink()  # proves the resume does not rebuild the map
    ctx = FakeCtx(state={"step": 3, "new_key_id": NEW_ID, "old_key_id": OLD_ID, **counts})
    outcome = run_rekey(env.cfg, _ctx(ctx), now=NOW)
    assert outcome.status == "done"
    assert not env.map_path.exists()
    assert env.key("redact.hmac_key.next") is None
    assert [r["content_hash"] for r in env.label_rows("human")] == [env.new["sn:incident:INC1"]]
    assert json.loads(env.marker.read_text("utf-8"))["new_key_id"] == NEW_ID


def test_ut10_70_resume_skips_finished_steps(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-70 a saved step 3 of the same new key skips steps 2 and 3 on the rerun."""
    env = rekey_env
    _plant_labels(env)
    rekey.build_rekey_map(
        env.cfg, redactor(env.cfg, NEW_KEY), out_path=env.map_path, ctx=_ctx(FakeCtx())
    )
    counts = rekey_labels(env.labels, env.map_path)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise AssertionError

    monkeypatch.setattr(rekey, "build_rekey_map", boom)
    monkeypatch.setattr(rekey, "rekey_labels", boom)
    state = {"step": 3, "new_key_id": NEW_ID, "old_key_id": OLD_ID, **counts}
    outcome = run_rekey(env.cfg, _ctx(FakeCtx(state=dict(state))), now=NOW)
    assert outcome.result["counts"] == counts
    assert env.key("redact.hmac_key") == NEW_KEY


# --- UT10-70 build_rekey_map ---------------------------------------------------------------


class _FlakyRedactor(Redactor):
    """Fails on any text that mentions alice (``__slots__`` forbid patching an instance)."""

    def redact(self, text: str | None) -> RedactionResult | None:
        if text is not None and "alice" in text:
            msg = "EMAIL"
            raise RedactionFailed(msg)
        return super().redact(text)


def test_ut10_70_map_failed_record_has_null_new_hash(rekey_env: RekeyEnv) -> None:
    """UT10-70 a record whose redaction fails gets new_content_hash NULL and is counted."""
    env = rekey_env
    red = _FlakyRedactor(env.cfg.security.redaction, bytes.fromhex(NEW_KEY))
    with structlog.testing.capture_logs() as logs:
        n = build_rekey_map(env.cfg, red, out_path=env.map_path, ctx=_ctx(FakeCtx()))
    assert n == len(env.old)
    rows = {r["record_id"]: r["new_content_hash"] for r in pq.read_table(env.map_path).to_pylist()}
    assert rows["sn:incident:INC1"] is None
    assert rows["sn:change:CHG1"] == env.new["sn:change:CHG1"]
    failed = [e for e in logs if e["event"] == "rekey.map.redaction_failed"]
    assert len(failed) == 1
    assert failed[0]["failed"] == 1
    assert "alice" not in repr(logs)


def test_ut10_70_map_missing_tables_is_schema_violation(rekey_env: RekeyEnv) -> None:
    """UT10-70 a CURRENT warehouse without the text tables raises SchemaViolation."""
    env = rekey_env
    folder = env.data / "warehouse"
    other = "20260921-120000-BBBBBB"
    duckdb.connect(str(folder / f"wh-{other}.duckdb")).close()
    (folder / "CURRENT").write_text(f"{other}\n", encoding="ascii")
    with pytest.raises(SchemaViolation):
        build_rekey_map(
            env.cfg, redactor(env.cfg, NEW_KEY), out_path=env.map_path, ctx=_ctx(FakeCtx())
        )
    assert not env.map_path.exists()
    assert not list(env.map_path.parent.glob(".*"))


def test_ut10_70_map_verification_failure_keeps_no_map(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-70 a written map whose re-read row count differs is discarded (FatalError)."""
    env = rekey_env

    class _Meta:
        num_rows = 0

    monkeypatch.setattr(rekey.pq, "read_metadata", lambda _p: _Meta())
    with pytest.raises(FatalError, match="verification"):
        build_rekey_map(
            env.cfg, redactor(env.cfg, NEW_KEY), out_path=env.map_path, ctx=_ctx(FakeCtx())
        )
    assert not env.map_path.exists()
    assert not list(env.map_path.parent.glob(".*"))


# --- UT10-70 rekey_labels ------------------------------------------------------------------


def _map(path: Path, rows: list[tuple[str, str, str | None]]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    names = ("record_id", "old_content_hash", "new_content_hash")
    pq.write_table(
        pa.table({n: pa.array([r[i] for r in rows], pa.string()) for i, n in enumerate(names)}),
        path,
    )
    return path


def test_ut10_70_rekey_labels_counts_and_staging(rekey_env: RekeyEnv) -> None:
    """UT10-70 one new hash -> replaced, several -> one row each (ambiguous), none -> kept_old;
    NULL new hashes ignored; rewrites are staged (live parts unchanged until committed)."""
    env = rekey_env
    rows: list[tuple[str, str, str | None]] = [
        ("r1", "a" * 32, "1" * 32),
        ("r2", "b" * 32, "2" * 32),
        ("r3", "b" * 32, "3" * 32),
        ("r4", "c" * 32, None),
    ]
    mpath = _map(env.data / "cache" / "rekey" / f"{NEW_ID}.parquet", rows)
    part = write_labels(env, "human", "a", [("r1", "a" * 32), ("r2", "b" * 32), ("r4", "c" * 32)])
    write_labels(env, "teacher", "a", [("r9", "9" * 32)])  # nothing changes: not staged
    before = part.read_bytes()
    counts = rekey_labels(env.labels, mpath)
    assert counts == {"rekeyed": 1, "kept_old": 2, "ambiguous": 1}
    assert part.read_bytes() == before
    staged = sorted(p.name for p in env.labels.rglob(".*"))
    assert staged == [f".part-a.parquet.rekey-{NEW_ID}"]
    assert rekey.commit_labels(env.labels, NEW_ID) == 1
    got = [(r["record_id"], r["content_hash"]) for r in env.label_rows("human")]
    assert got == [("r1", "1" * 32), ("r2", "2" * 32), ("r2", "3" * 32), ("r4", "c" * 32)]
    assert rekey.commit_labels(env.labels, NEW_ID) == 0


def test_ut10_70_rekey_labels_os_error_is_store_busy(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-70 an OSError while rewriting a label part raises StoreBusy naming the file only."""
    env = rekey_env
    mpath = _map(env.data / "cache" / "rekey" / f"{NEW_ID}.parquet", [("r1", "a" * 32, "1" * 32)])
    write_labels(env, "gold", "a", [("r1", "a" * 32)])

    def deny(*_args: Any, **_kwargs: Any) -> None:
        raise PermissionError

    monkeypatch.setattr(rekey.os, "replace", deny)
    with pytest.raises(StoreBusy, match="label part"):
        rekey_labels(env.labels, mpath)
    assert not list(env.labels.rglob("*.tmp"))


def test_ut10_70_label_part_without_hash_column_is_schema_violation(rekey_env: RekeyEnv) -> None:
    """UT10-70 a label part without ``content_hash`` raises SchemaViolation."""
    env = rekey_env
    mpath = _map(env.data / "cache" / "rekey" / f"{NEW_ID}.parquet", [("r1", "a" * 32, "1" * 32)])
    folder = env.labels / QSV / "human"
    folder.mkdir(parents=True)
    pq.write_table(pa.table({"record_id": ["r1"]}), folder / "part-x.parquet")
    with pytest.raises(SchemaViolation, match=r"part-x\.parquet"):
        rekey_labels(env.labels, mpath)


def test_ut10_70_staged_verification_mismatch_is_fatal(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT10-70 a staged part whose re-read hashes differ is removed and the job fails."""
    env = rekey_env
    mpath = _map(env.data / "cache" / "rekey" / f"{NEW_ID}.parquet", [("r1", "a" * 32, "1" * 32)])
    part = write_labels(env, "human", "a", [("r1", "a" * 32)])
    real_read = rekey.pq.read_table

    def tampered(path: Any, **kwargs: Any) -> pa.Table:
        table = real_read(path, **kwargs)
        if str(path).endswith(".tmp"):
            return table.slice(0, 0)
        return table

    monkeypatch.setattr(rekey.pq, "read_table", tampered)
    with pytest.raises(FatalError, match="verification"):
        rekey_labels(env.labels, mpath)
    assert sorted(p.name for p in part.parent.iterdir()) == [part.name]


# --- maintenance table ---------------------------------------------------------------------


def test_ut10_70_maintenance_rekey_routes_to_run_rekey(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT10-70 ``maintenance`` ``{"action": "rekey"}`` calls run_rekey(get_config(), ctx, now)."""
    seen: list[tuple[object, object, object]] = []
    own = JobOutcome(status="done", result={"x": 1})

    def fake(cfg: object, ctx: object, *, now: object) -> JobOutcome:
        seen.append((cfg, ctx, now))
        return own

    sentinel = object()
    monkeypatch.setattr("herness.admin.maintenance.run_rekey", fake)
    monkeypatch.setattr("herness.admin.maintenance.get_config", lambda: sentinel)
    monkeypatch.setattr("herness.admin.maintenance.clock.now", lambda: NOW)
    ctx = FakeCtx()
    assert handle_maintenance(_ctx(ctx), memory_purge=lambda _r: 0) is own
    assert seen == [(sentinel, ctx, NOW)]
