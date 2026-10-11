"""Controller verification of ``herness.cli.worker_bootstrap`` in a cold process (T09-27).

A fresh interpreter, a temporary config tree and data directory, no network and no model call:
the bootstrap runs twice and every job kind then resolves to a handler that did not change.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config

pytestmark = pytest.mark.integration

CHILD = """
import json, sys
from typing import get_args
import keyring
from tests.support.fake_keyring import MemoryKeyring

store = MemoryKeyring()  # hermetic: no OS credential store
store.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
keyring.set_keyring(store)
import herness.cli as cli
from herness._cli import _worker_boot
from herness.core.jobs.handlers import resolve_handler
from herness.core.types import JobKind

kinds = get_args(JobKind.__value__)
cli.worker_bootstrap()
first = {k: resolve_handler(k) for k in kinds}
cli.worker_bootstrap()  # the process flag: nothing runs
_worker_boot._Booted.state = None  # force every owner to re-register: identities must match
cli.worker_bootstrap()
second = {k: resolve_handler(k) for k in kinds}
print(json.dumps({"kinds": list(kinds), "same": all(first[k] is second[k] for k in kinds)}))
"""


def test_cv_t09_27_cold_process_bootstrap_twice_resolves_every_kind(tmp_path: Path) -> None:
    """CV T09-27 a cold process bootstraps twice; all ten kinds resolve, none re-registered."""
    cfg = write_full_config(tmp_path)
    data = tmp_path / "data"
    data.mkdir()
    env = {
        **os.environ,
        "HERNESS_CONFIG_DIR": str(cfg),
        "HERNESS_DATA_DIR": str(data),
        "HERNESS_PROFILE": "hybrid",  # local + egress off: MemoryStore refuses off-network clients
        "HERNESS_SET_OVERRIDES": "[]",
        "PYTHONUTF8": "1",
    }
    done = subprocess.run(  # noqa: S603 - list argv, own interpreter
        [sys.executable, "-c", CHILD],
        env=env,
        cwd=tmp_path,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    result = json.loads(done.stdout.strip().splitlines()[-1])
    assert len(result["kinds"]) == 10
    assert result["same"] is True
