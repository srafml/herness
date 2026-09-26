"""Fixtures for enrich integration tests (impl 03 §11)."""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config

# Built at runtime so that the detect-secrets baseline does not change.
REDACT_KEY = bytes(range(32))


@pytest.fixture
def redaction_on(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """A loaded config whose redaction key is in the (fake) keyring: real ``redact_table``."""
    cfg_dir = write_full_config(tmp_path / "cfg")
    fake_keyring.store[("herness", "redact.hmac_key")] = REDACT_KEY.hex()
    config.init_config("local", config_dir=cfg_dir, env={})
