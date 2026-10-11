"""IT09-30 ``herness sync files --inline`` in a cold CLI process (T09-29, U09-103).

A fresh interpreter runs ``herness.cli.main`` on a temporary config and data directory with
the ``local`` profile: no test registers a job handler, so the inline helper's bootstrap must
register the owners' handlers itself. Only the process seams are faked: the OS keyring, the
OS user (admin ``root``) and elevation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support.cli_env import CliEnv
from tests.support.sync_env import FILES_SOURCES_YAML, drop_inbox

pytestmark = pytest.mark.integration

CHILD = """
import sys
import keyring
from tests.support.fake_keyring import MemoryKeyring

store = MemoryKeyring()  # hermetic: no OS credential store
store.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
store.store[("herness", "ui_user_ref_key")] = "cold-process-test-key-not-secret"
keyring.set_keyring(store)
from herness import cli
from herness._cli import identity

identity._credential_user = lambda: "root"
identity._is_elevated = lambda: True
cli.main(sys.argv[1:])
"""


def _run(root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PYTHONUTF8": "1"}
    for name in ("HERNESS_CONFIG_DIR", "HERNESS_DATA_DIR", "HERNESS_PROFILE"):
        env.pop(name, None)
    argv = ["--config-dir", str(root / "config"), "--profile", "local", "--json", *args]
    return subprocess.run(  # noqa: S603 - list argv, own interpreter
        [sys.executable, "-c", CHILD, *argv],
        env=env,
        cwd=root,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )


def test_it09_30_cold_process_sync_inline_runs_to_done(tmp_path: Path) -> None:
    """IT09-30 `herness sync files --inline` in a cold process: exit 0, job done, parquet
    written; no handler is registered by anyone but the CLI itself (U09-103)."""
    cfg = CliEnv(tmp_path).write_config(admins=["root"])
    (cfg / "sources.yaml").write_text(FILES_SOURCES_YAML, encoding="utf-8")
    drop_inbox(tmp_path / "data")
    init = _run(tmp_path, "init")  # a fresh store: creates the schema; existing files stay
    assert init.returncode == 0, init.stderr[-3000:]
    done = _run(tmp_path, "sync", "files", "--inline")
    assert done.returncode == 0, done.stderr[-3000:]
    envelope = json.loads(done.stdout.strip().splitlines()[-1])
    assert envelope["ok"] is True
    assert envelope["data"]["status"] == "done"
    assert list((tmp_path / "data" / "raw" / "files" / "teams").rglob("*.parquet"))
