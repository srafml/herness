"""T07-25: the memory store builds under the shipped ``local`` profile with egress off."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import register_checked_names, write_repo_config
from tests.support.egress_servers import record_connects
from tests.support.fake_keyring import MemoryKeyring

import herness.harness.memory as memory_pkg
from herness.core import config as c
from herness.core import egress as eg
from herness.core import registry
from herness.harness.memory import MemoryStore, get_memory_store

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    memory_pkg._reset_memory_store()
    c.reset_config()
    registry.reset_registry()


def test_ut07_93_get_memory_store_builds_under_shipped_local_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """UT07-93 the shipped local profile (egress off) builds the store; zero outbound calls."""
    fake_keyring.store[("herness", "redact.hmac_key")] = bytes(range(32)).hex()
    cfg_dir = write_repo_config(tmp_path)
    monkeypatch.chdir(tmp_path)
    cfg = c.init_config("local", config_dir=cfg_dir, env={})
    assert cfg.security.egress.enabled is False
    connects = record_connects(monkeypatch)
    guard_calls: list[str] = []
    monkeypatch.setattr(eg, "get_guard", lambda: guard_calls.append("get_guard"))

    store = get_memory_store()

    assert isinstance(store, MemoryStore)
    assert connects == []
    assert guard_calls == []
