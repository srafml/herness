"""ST03-07 (TH03-05): tampered Laya weights are refused on load and on accept (T03-14)."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version

from herness.core.errors import ConfigError, ModelUnavailable
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.laya_models import verify_model_dir, write_current
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import LayaSettings

pytestmark = pytest.mark.unit

_V1 = "laya-20261004-1"


@pytest.fixture
def paths(tmp_path: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=tmp_path.resolve(),
        embedding_path="data/models/e",
        laya_current_file="data/models/laya/CURRENT",
    )


def _flip_one_byte(path: Path) -> None:
    before = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0x01
    path.write_bytes(bytes(data))
    # The write moves mtime; pin a distinct value so a coarse filesystem clock cannot keep
    # the (path, size, mtime_ns) hash memo key unchanged within this one test process.
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))


def test_st03_07_flipped_byte_refused_on_load_and_accept(
    paths: EnrichPaths, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-07 flip one byte of model.safetensors: load and accept both refuse."""
    monkeypatch.setenv("HF_HUB_OFFLINE", "0")  # recorded, so monkeypatch restores it
    monkeypatch.delenv("HF_HUB_OFFLINE")
    agent = FakeLayaAgent()
    module = fake_laya_module(agent)
    monkeypatch.setitem(sys.modules, "laya", module)
    directory = write_laya_version(paths.laya_root(), _V1)
    write_current(paths, _V1)
    assert verify_model_dir(paths, _V1, require_status=frozenset({"accepted"})).version == _V1

    _flip_one_byte(directory / "model.safetensors")

    with pytest.raises(ConfigError, match=f"^{_V1}: weight hash mismatch$") as info:
        verify_model_dir(paths, _V1, require_status=frozenset({"accepted"}))
    assert "safetensors" not in str(info.value)  # names the check, never file contents
    decider = LayaDecider(LayaSettings(device="cpu"), paths=paths, version=_V1)
    with pytest.raises(ConfigError, match="weight hash mismatch"):
        decider.load()
    with pytest.raises(ModelUnavailable, match="weight hash mismatch"):
        decider.health()
    assert module.loads == []  # type: ignore[attr-defined]
    assert agent.to_calls == []
