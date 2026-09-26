"""Fault tests for impl 10 security paths (FT10-01 T10-05, FT10-05 T10-06, FT10-07 T10-11)."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pytest
from keyring.errors import KeyringError
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import _redact_pool as redact_pool
from herness.core import audit as a
from herness.core import config as c
from herness.core import redact, secrets
from herness.core.errors import ConfigError, FatalError, RetryableError, StoreBusy
from herness.core.logging import configure_logging, reset_logging

pytestmark = pytest.mark.fault

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
