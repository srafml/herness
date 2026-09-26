"""Fault tests for impl 10 security paths (FT10-01 T10-05, FT10-05 T10-06)."""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from keyring.errors import KeyringError
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import audit as a
from herness.core import config as c
from herness.core import secrets
from herness.core.errors import ConfigError, FatalError

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
