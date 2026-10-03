"""Shared fixture of the queue and handler tests (impl 08 §11 `ops_db`, T08-12).

`jobs_db`: a migrated ops store bound as the resilience and jobs backends, the full test
config (design 08 §7 defaults) and a fixed-key redactor; the backends are bound by
`bind_core_backends` (U08-98).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState
from herness.core.settings import RedactionConfig
from herness.store.ops import bind_core_backends


@pytest.fixture
def jobs_db(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[ProcessState]:
    """The §11 `ops_db` for the queue API: store, config, redactor and bound backends."""
    del ops_store, fake_keyring
    c.reset_config()
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    bind_core_backends()
    yield reset_process_state
    c.reset_config()
