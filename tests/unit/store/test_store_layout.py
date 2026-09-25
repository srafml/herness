"""Tests for herness.store.layout (U02-06, U02-07)."""

import dataclasses
import sys
import types
from pathlib import Path

import pytest

from herness.store.layout import DataLayout, data_layout

pytestmark = pytest.mark.unit


def _paths(layout: DataLayout) -> tuple[Path, ...]:
    return (layout.raw, layout.warehouse, layout.ops_db, layout.vectors)


@pytest.mark.parametrize(
    ("root", "synth"),
    [("D:/x/data/synth/7-tiny", True), ("D:/x/data", False)],
)
def test_ut02_66_layout_children_and_synth_marker(root: str, synth: bool) -> None:
    """UT02-66 child paths under the resolved root; synth_marker true, false."""
    layout = data_layout(root=Path(root))
    resolved = Path(root).resolve(strict=False)
    assert layout.root == resolved
    assert layout.raw == resolved / "raw"
    assert layout.warehouse == resolved / "warehouse"
    assert layout.ops_db == resolved / "ops.sqlite"
    assert layout.vectors == resolved / "vectors"
    assert all(p.parent == resolved for p in _paths(layout))
    assert layout.synth_marker is synth
    assert layout == DataLayout.from_root(Path(root))


def test_ut02_66_synth_marker_rules(tmp_path: Path) -> None:
    """UT02-66 marker needs the consecutive pair data, synth (case-insensitive)."""
    assert DataLayout.from_root(tmp_path / "DATA" / "Synth" / "3").synth_marker
    assert not DataLayout.from_root(tmp_path / "synth" / "data").synth_marker
    assert not DataLayout.from_root(tmp_path / "data" / "x" / "synth").synth_marker


def test_ut02_66_layout_is_frozen_and_creates_nothing(tmp_path: Path) -> None:
    """UT02-66 DataLayout is frozen and from_root creates no directories."""
    layout = DataLayout.from_root(tmp_path / "data")
    assert not (tmp_path / "data").exists()
    with pytest.raises(dataclasses.FrozenInstanceError):
        layout.root = tmp_path  # type: ignore[misc]


def test_ut02_66_relative_root_resolves_against_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-66 a relative root resolves against the current working directory."""
    monkeypatch.chdir(tmp_path)
    assert data_layout(root=Path("data")).root == (tmp_path / "data").resolve()


def test_ut02_66_layout_from_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-66 data_layout reads paths.data from cfg, or from get_config() without cfg."""
    cfg = types.SimpleNamespace(paths=types.SimpleNamespace(data=tmp_path / "data"))
    assert data_layout(cfg=cfg).root == (tmp_path / "data").resolve()
    assert data_layout(cfg=cfg, root=tmp_path / "other").root == (tmp_path / "other").resolve()
    fake = types.ModuleType("herness.core.config")
    fake.get_config = lambda: cfg  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "herness.core.config", fake)
    assert data_layout().root == (tmp_path / "data").resolve()
