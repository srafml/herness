"""Fault tests for impl 10 security paths (FT10-01 T10-05, FT10-05 T10-06, FT10-07 T10-11,
FT10-08 T10-16, FT10-02 T10-30)."""

from __future__ import annotations

import datetime
import errno
import json
import os
import socket
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from keyring.errors import KeyringError
from tests.support.config_tree import write_full_config
from tests.support.egress_harness import API, audit_fields, make_guard
from tests.support.egress_harness import load as load_egress
from tests.support.fake_keyring import MemoryKeyring
from tests.support.rekey_env import (
    NEW_KEY,
    OLD_ID,
    OLD_KEY,
    FakeCtx,
    RekeyEnv,
    open_rekey_env,
    write_labels,
)

from herness.admin import rekey as rekey_module
from herness.admin.rekey import run_rekey
from herness.core import _redact_pool as redact_pool
from herness.core import audit as a
from herness.core import config as c
from herness.core import redact, secrets
from herness.core.errors import ConfigError, EgressBlocked, FatalError, RetryableError, StoreBusy
from herness.core.jobs.ports import JobContext
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.fault

REKEY_NOW = datetime.datetime(2026, 9, 26, 19, 0, tzinfo=datetime.UTC)

REPO = Path(__file__).resolve().parents[2]
HOLDER = """
import sys
from pathlib import Path
from herness.core.audit import log_lock
with log_lock(Path(sys.argv[1])):
    sys.stdout.write("locked\\n")
    sys.stdout.flush()
    sys.stdin.readline()
"""


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()


def test_ft10_01_lock_held_elsewhere_blocks_the_audited_action(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT10-01 audit lock held by another process: FatalError after 10 s; no secret set."""
    cfg = c.init_config(config_dir=write_full_config(tmp_path), env={})
    logs = cfg.paths.logs
    logs.mkdir(parents=True)
    now = [100.0]
    monkeypatch.setattr(a, "_monotonic", lambda: now[0])
    monkeypatch.setattr(a, "_sleep", lambda s: now.__setitem__(0, now[0] + s))
    backend: dict[str, str] = {}

    def set_secret(name: str, value: str) -> None:  # U10-31 order: audit first, then store
        a.audit("admin_action", "system", action="secret_set", target=name)
        backend[name] = value

    holder = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-c", HOLDER, str(logs / ".audit.lock")],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        cwd=REPO,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline() == "locked\n"
        with pytest.raises(FatalError, match=r"^audit write failed: admin_action$"):
            set_secret("snow.token", "v")
    finally:
        holder.communicate("\n", timeout=30)
    assert now[0] - 100.0 >= 10.0
    assert backend == {}
    assert not list(logs.glob("audit-*.jsonl"))
    set_secret("snow.token", "v")  # lock released: the action proceeds
    assert backend == {"snow.token": "v"}


def test_ft10_05_keyring_failure_is_config_error_with_hint(
    fake_keyring: MemoryKeyring, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT10-05 keyring raises (KeyringError or any error): ConfigError, hint, log event."""
    events: list[tuple[str, dict[str, object]]] = []

    class _Log:
        def error(self, event: str, **kw: object) -> None:
            events.append((event, kw))

    monkeypatch.setattr(secrets, "_log", _Log())
    fake_keyring.store[("herness", "vllm.api_key")] = "Fault-Sentinel-Value-1"

    class _WinError(Exception):  # stands in for a raw pywintypes.error from WinVaultKeyring
        pass

    errors = (
        KeyringError("locked vault Fault-Sentinel-Value-1"),
        RuntimeError("no backend"),
        _WinError(5, "Access is denied", "Fault-Sentinel-Value-1"),
    )
    for error in errors:
        fake_keyring.error = error
        for call in (
            lambda: secrets.resolve("secret:vllm.api_key"),
            lambda: secrets.exists("vllm.api_key"),
        ):
            with pytest.raises(ConfigError, match=r"^secret backend unavailable: keyring$") as exc:
                call()
            assert exc.value.hint == "run as the account that owns the credential"
            assert exc.value.__cause__ is None
            assert "Fault-Sentinel" not in str(exc.value)
    assert len(events) == 6
    assert {e[0] for e in events} == {"secrets.backend.unavailable"}
    assert all(e[1]["backend"] == "keyring" for e in events)
    assert all("Fault-Sentinel" not in str(e[1]) for e in events)
    assert "Fault-Sentinel-Value-1" not in secrets.known_values()


# --- FT10-07 a redaction worker process is killed (T10-11) -------------------------------------


def _killed_worker(chunk: object) -> object:
    """Stand-in for ``_redact_pool._redact_chunk``: the worker process dies at once."""
    os._exit(9)


def test_ft10_07_killed_redaction_worker_raises_store_busy_and_no_table(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """FT10-07 a redaction worker process dies: StoreBusy (retryable); no table returned."""
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8")
    herness.write_text(text.replace("security:\n", "security:\n  secrets: {backend: dotenv}\n"))
    (tmp_path / ".env").write_text(f"HERNESS_SECRET__REDACT_HMAC_KEY={bytes(32).hex()}\n", "utf-8")
    monkeypatch.setenv("HERNESS_ENV", "dev")
    c.init_config("local", config_dir=cfg_dir)
    monkeypatch.setattr(redact_pool, "_redact_chunk", _killed_worker)  # pickled by reference
    rows = redact_pool.CHUNK_ROWS + 1  # two chunks: the pool path
    tbl = pa.table({"record_id": [str(i) for i in range(rows)], "t": ["mail a@b.test"] * rows})
    configure_logging("INFO")
    capsys.readouterr()
    result = None
    try:
        with pytest.raises(StoreBusy, match=r"^redaction worker crashed$") as caught:
            result = redact.redact_table(tbl, ["t"], workers=2)
    finally:
        reset_logging()
    assert result is None
    assert isinstance(caught.value, RetryableError)
    lines = [json.loads(x) for x in capsys.readouterr().err.splitlines() if x.startswith("{")]
    events = {line["event"]: line for line in lines}
    assert events["redact.table.failed"]["error_type"] == "BrokenProcessPool"
    assert "redact.table.completed" not in events
    assert all("a@b.test" not in json.dumps(line) for line in lines)


@pytest.mark.parametrize("body", [b'{"metric": "mttr_hours"}', b"mail john@corp.com"])
def test_ft10_08_egress_log_disk_full_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring, body: bytes
) -> None:
    """FT10-08 OSError (disk full) writing the egress log: egress_log_failed; no socket opened."""
    cfg = load_egress(tmp_path, "hybrid")
    real_open = Path.open

    def full_disk(self: Path, mode: str = "r", *args: Any, **kw: Any) -> Any:
        if self.name.startswith("egress-") and "a" in mode:
            raise OSError(errno.ENOSPC, "No space left on device")
        return real_open(self, mode, *args, **kw)

    connects: list[object] = []
    monkeypatch.setattr(Path, "open", full_disk)
    monkeypatch.setattr(socket.socket, "connect", lambda _self, addr: connects.append(addr))
    with pytest.raises(EgressBlocked) as caught:
        make_guard(cfg).check(API, body, "reasoning_final", "aggregated_evidence")
    assert caught.value.reason == "egress_log_failed"
    assert connects == []
    assert not list(cfg.paths.logs.glob("egress-*.jsonl"))
    assert audit_fields(cfg.paths.logs) == [
        {"egress_id": caught.value.egress_id, "reason": "egress_log_failed"}
    ]


@pytest.fixture
def rekey_env(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[RekeyEnv]:
    """The planted rekey environment of ``tests.support.rekey_env``."""
    with open_rekey_env(tmp_path, fake_keyring) as env:
        yield env


# --- FT10-02 rekey killed before the key swap (T10-30) ---------------------------------------


class _Killed(BaseException):
    """Stands in for the worker process dying at a chosen point of the rekey job."""


def _kill_at(monkeypatch: pytest.MonkeyPatch, attr: str, name: str) -> None:
    real = getattr(rekey_module, attr)

    def die(secret: str, *args: Any, **kwargs: Any) -> Any:
        if secret == name:
            raise _Killed
        return real(secret, *args, **kwargs)

    monkeypatch.setattr(rekey_module, attr, die)


def test_ft10_02_killed_after_step_3_keeps_old_key_until_rerun(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT10-02 rekey killed after step 3, before 4a: old key active, hashes and labels
    unchanged; the rerun swaps the key and re-keys the labels."""
    env = rekey_env
    write_labels(env, "human", "a", [("sn:incident:INC1", env.old["sn:incident:INC1"])])
    write_labels(env, "gold", "a", [("sn:change:CHG1", env.old["sn:change:CHG1"])])
    labels_before = env.label_bytes()
    warehouse = next((env.data / "warehouse").glob("wh-*.duckdb"))
    wh_before = warehouse.read_bytes()
    ctx = FakeCtx()
    with monkeypatch.context() as patch:
        _kill_at(patch, "set_secret", "redact.hmac_key")  # step 4a
        with pytest.raises(_Killed):
            run_rekey(env.cfg, cast("JobContext", ctx), now=REKEY_NOW)
    assert ctx.state["step"] == 3
    assert (env.key("redact.hmac_key"), env.key("redact.hmac_key.next")) == (OLD_KEY, NEW_KEY)
    live = {k: v for k, v in env.label_bytes().items() if not k.rsplit("/", 1)[-1].startswith(".")}
    assert live == labels_before  # labels unchanged before the rerun
    assert warehouse.read_bytes() == wh_before  # hashes unchanged
    assert not env.marker.exists()
    redact.reset_redactor()
    assert redact.get_redactor().key_id == OLD_ID  # old key still active
    outcome = run_rekey(env.cfg, cast("JobContext", ctx), now=REKEY_NOW)  # rerun
    assert outcome.status == "done"
    assert (env.key("redact.hmac_key"), env.key("redact.hmac_key.next")) == (NEW_KEY, None)
    assert [r["content_hash"] for r in env.label_rows("human")] == [env.new["sn:incident:INC1"]]
    assert [r["content_hash"] for r in env.label_rows("gold")] == [env.new["sn:change:CHG1"]]
    assert env.marker.exists()


def test_ft10_02_killed_after_swap_rerun_finishes(
    rekey_env: RekeyEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT10-02 killed right after 4a (new key active, staged labels not yet swapped in, .next
    kept), or after the label swap: the rerun resumes at 4b and finishes the rotation."""
    env = rekey_env
    write_labels(env, "teacher", "a", [("sn:problem:PRB1", env.old["sn:problem:PRB1"])])
    ctx = FakeCtx()

    def die(*_args: Any) -> int:
        raise _Killed

    with monkeypatch.context() as patch:
        patch.setattr(rekey_module, "commit_labels", die)  # right after 4a
        with pytest.raises(_Killed):
            run_rekey(env.cfg, cast("JobContext", ctx), now=REKEY_NOW)
    assert (env.key("redact.hmac_key"), env.key("redact.hmac_key.next")) == (NEW_KEY, NEW_KEY)
    assert [r["content_hash"] for r in env.label_rows("teacher")] == [env.old["sn:problem:PRB1"]]
    with monkeypatch.context() as patch:
        _kill_at(patch, "delete_secret", "redact.hmac_key.next")  # labels swapped, then 4b
        with pytest.raises(_Killed):
            run_rekey(env.cfg, cast("JobContext", ctx), now=REKEY_NOW)
    assert [r["content_hash"] for r in env.label_rows("teacher")] == [env.new["sn:problem:PRB1"]]
    outcome = run_rekey(env.cfg, cast("JobContext", ctx), now=REKEY_NOW)
    assert outcome.status == "done"
    assert env.key("redact.hmac_key.next") is None
    assert [r["content_hash"] for r in env.label_rows("teacher")] == [env.new["sn:problem:PRB1"]]
    actions = [cast("dict[str, object]", ln["fields"]).get("action") for ln in env.audit_lines()]
    assert "redact_rekey" in actions
    assert not [p for p in env.labels.rglob("*") if p.name.startswith(".")]  # nothing staged
