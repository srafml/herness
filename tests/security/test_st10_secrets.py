"""Security tests for the secrets backends (impl 10 ST10-26, TH10-11, T10-06)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core import secrets
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

REFUSED = r"^dotenv secrets backend is refused outside dev and synth$"


@pytest.fixture(autouse=True)
def _reset(fake_keyring: MemoryKeyring) -> Iterator[None]:
    yield
    c.reset_config()


def test_st10_26_dotenv_backend_refused_in_local_without_dev(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, fake_keyring: MemoryKeyring
) -> None:
    """ST10-26 backend: dotenv with profile local and no HERNESS_ENV: ConfigError on every use."""
    cfg_dir = write_full_config(tmp_path)
    herness = cfg_dir / "herness.yaml"
    text = herness.read_text("utf-8")
    herness.write_text(text.replace("security:\n", "security:\n  secrets: {backend: dotenv}\n"))
    (tmp_path / ".env").write_text("HERNESS_SECRET__VLLM_API_KEY=Dotenv-Bypass-1\n", "utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("HERNESS_ENV", raising=False)
    cfg = c.init_config("local", config_dir=cfg_dir, env={})
    assert cfg.security.secrets.backend == "dotenv"
    fake_keyring.store[("herness", "vllm.api_key")] = "Keyring-Value-1"
    calls = (
        lambda: secrets.resolve("secret:vllm.api_key"),
        lambda: secrets.resolve_json("vllm.api_key"),
        lambda: secrets.exists("vllm.api_key"),
        lambda: secrets.set_secret("vllm.api_key", "Long-Enough-1", actor="system"),
        lambda: secrets.delete_secret("vllm.api_key", actor="system"),
    )
    for call in calls:
        with pytest.raises(ConfigError, match=REFUSED):
            call()
    assert secrets.known_values() == frozenset()
    assert fake_keyring.store == {("herness", "vllm.api_key"): "Keyring-Value-1"}
    assert not list(Path(cfg.paths.logs).glob("audit-*.jsonl"))
