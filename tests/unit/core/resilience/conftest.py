"""Fixtures for the impl 08 resilience unit tests: a loaded config, a test redactor and
`ops_db` (a migrated ops store bound as the resilience backend, impl 08 §11)."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.settings import RedactionConfig
from herness.store.ops.resilience import SqliteResilienceBackend


@pytest.fixture
def herness_cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A full config (design 08 §7 resilience defaults) cached for `get_config`."""
    c.reset_config()
    yield c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


@pytest.fixture
def test_redactor(monkeypatch: pytest.MonkeyPatch) -> r.Redactor:
    """A process redactor with a fixed key and no directory names (no config needed)."""
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    return redactor


@pytest.fixture
def ops_db(ops_store: OpsStoreHandle, reset_process_state: ProcessState) -> OpsStoreHandle:
    """A fresh migrated ops store bound as the resilience backend (the `ops_db` of §11;
    `bind_core_backends`, U08-98, is a later card, so the backend is bound directly)."""
    del reset_process_state
    bind_ops_backend(SqliteResilienceBackend())
    return ops_store
