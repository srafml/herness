"""Tests for herness.enrich.layout (U03-12, U03-13; T03-03).

``HernessConfig`` (T10-03) is not in the tree yet: ``EnrichPaths.from_config`` is exercised
with a small fake object exposing the three attributes it reads (real settings leaves).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from herness.core.errors import ConfigError
from herness.enrich.layout import EnrichPaths, resolve_data_path
from herness.enrich.settings import DecidersSettings, EmbeddingSettings

pytestmark = pytest.mark.unit


@dataclass(frozen=True)
class _Paths:
    data: Path


@dataclass(frozen=True)
class _Decisions:
    embedding: EmbeddingSettings = field(
        default_factory=lambda: EmbeddingSettings(path="data/models/bge-m3/rev1/")
    )


@dataclass(frozen=True)
class _Models:
    deciders: DecidersSettings = field(default_factory=DecidersSettings)


@dataclass(frozen=True)
class _FakeConfig:
    paths: _Paths
    decisions: _Decisions = field(default_factory=_Decisions)
    models: _Models = field(default_factory=_Models)


def _paths(root: Path) -> EnrichPaths:
    return EnrichPaths(
        data_root=root.resolve(),
        embedding_path="data/models/e",
        laya_current_file="data/models/laya/CURRENT",
    )


def test_ut03_10_relative_data_path_resolves_under_root(tmp_path: Path) -> None:
    """UT03-10 `data/models/x` resolves to <root>/models/x; other relative paths join too."""
    root = tmp_path.resolve()
    assert resolve_data_path("data/models/x", data_root=tmp_path) == root / "models" / "x"
    assert resolve_data_path(Path("models/y"), data_root=tmp_path) == root / "models" / "y"
    assert resolve_data_path("data", data_root=tmp_path) == root
    inside = root / "models" / "z"
    assert resolve_data_path(str(inside), data_root=tmp_path) == inside


@pytest.mark.parametrize("bad", ["../x", "data/../../x", "/etc/x"])
def test_ut03_10_paths_outside_root_rejected(tmp_path: Path, bad: str) -> None:
    """UT03-10 `../x` and `/etc/x` raise ConfigError("path outside data root: ...")."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ConfigError, match="path outside data root"):
        resolve_data_path(bad, data_root=root)


def test_ut03_10_absolute_path_outside_root_rejected(tmp_path: Path) -> None:
    """UT03-10 an absolute path elsewhere on disk is rejected."""
    root = tmp_path / "root"
    root.mkdir()
    with pytest.raises(ConfigError, match="path outside data root"):
        resolve_data_path(tmp_path / "other" / "x", data_root=root)


def test_ut03_10_symlink_out_of_root_rejected(tmp_path: Path) -> None:
    """UT03-10 a symlink inside the root pointing outside it is rejected."""
    root = tmp_path / "root"
    (root / "models").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    link = root / "models" / "escape"
    try:
        os.symlink(outside, link, target_is_directory=True)
    except OSError:
        pytest.skip("symlink creation needs privileges on this platform")
    with pytest.raises(ConfigError, match="path outside data root"):
        resolve_data_path("data/models/escape/x", data_root=root)


def test_ut03_10_junction_out_of_root_rejected(tmp_path: Path) -> None:
    """UT03-10 (Windows) a directory junction inside the root pointing outside is rejected."""
    winapi = pytest.importorskip("_winapi")
    root = tmp_path / "root"
    (root / "models").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    winapi.CreateJunction(str(outside), str(root / "models" / "escape"))
    with pytest.raises(ConfigError, match="path outside data root"):
        resolve_data_path("data/models/escape/x", data_root=root)


def test_ut03_11_relative_data_root_rejected() -> None:
    """UT03-11 (U03-13 precondition) a relative data root raises ConfigError."""
    with pytest.raises(ConfigError, match="data_root"):
        EnrichPaths(data_root=Path("rel"), embedding_path="e", laya_current_file="c")


def test_ut03_11_data_root_resolved_once(tmp_path: Path) -> None:
    """UT03-11 (U03-13) an absolute but unresolved root is resolved once, so every method
    returns paths from the same root; `resolve_data_path("")` is rejected."""
    paths = EnrichPaths(
        data_root=tmp_path / "sub" / "..", embedding_path="data/e", laya_current_file="data/c"
    )
    root = tmp_path.resolve()
    assert paths.data_root == root
    assert paths.pairs_dir() == root / "cache" / "pairs"
    assert paths.laya_current() == root / "c"
    assert paths.cluster_root("NULL") == root / "models" / "clusters" / "NULL"
    for empty in ("", "."):
        with pytest.raises(ConfigError, match="empty"):
            resolve_data_path(empty, data_root=root)


def test_ut03_11_path_methods_with_valid_identifiers(tmp_path: Path) -> None:
    """UT03-11 valid versions and deciders give the §4.2-§4.7 paths under the data root."""
    root = tmp_path.resolve()
    paths = EnrichPaths.from_config(_FakeConfig(paths=_Paths(data=tmp_path)))
    qsv = "qs-2026-10-01.1"
    assert paths.data_root == root
    assert paths.cache_dir(qsv) == root / "cache" / "decisions" / qsv
    assert paths.cache_partition(qsv, "openjev", "0.4.0/a+b") == (
        root / "cache" / "decisions" / qsv / "decider=openjev" / "decider_version=0.4.0%2Fa%2Bb"
    )
    assert paths.labels_dir(qsv, "gold") == root / "labels" / qsv / "gold"
    assert paths.laya_root() == root / "models" / "laya"
    assert paths.laya_dir("laya-20261004-1") == root / "models" / "laya" / "laya-20261004-1"
    assert paths.laya_current() == root / "models" / "laya" / "CURRENT"
    assert paths.calibration_file("llm", "m@1", qsv) == (
        root / "models" / "calibration" / "llm" / "m%401" / f"{qsv}.json"
    )
    assert paths.cluster_root("hdb-1") == root / "models" / "clusters" / "hdb-1"
    assert paths.cluster_snapshot("hdb-1", "snap_2") == (
        root / "models" / "clusters" / "hdb-1" / "snap_2"
    )
    assert paths.pairs_dir() == root / "cache" / "pairs"
    assert paths.embedding_model_dir() == root / "models" / "bge-m3" / "rev1"
    assert paths.vectors_dir() == root / "vectors"


@pytest.mark.parametrize(
    ("call", "argument"),
    [
        (lambda p: p.laya_dir("laya-../x"), "version"),
        (lambda p: p.laya_dir("laya-2026-1"), "version"),
        (lambda p: p.cache_dir("../../etc"), "qsv"),
        (lambda p: p.labels_dir("qs-2026-10-01", "bogus"), "kind"),
        (lambda p: p.cache_partition("qs-2026-10-01", "evil", "1"), "decider"),
        (lambda p: p.cache_partition("qs-2026-10-01", "laya", "a b"), "decider_version"),
        (lambda p: p.calibration_file("llm", "..", "qs-2026-10-01"), "decider_version"),
        (lambda p: p.calibration_file("evil", "1", "qs-2026-10-01"), "decider"),
        (lambda p: p.cluster_root("a/b"), "algorithm_version"),
        (lambda p: p.cluster_root(".."), "algorithm_version"),
        (lambda p: p.cluster_snapshot("v1", "."), "snapshot_id"),
        (lambda p: p.cluster_snapshot("v1", "x" * 97), "snapshot_id"),
        (lambda p: p.cluster_snapshot("v1", "..."), "snapshot_id"),
        (lambda p: p.cluster_snapshot("v1", "a."), "snapshot_id"),
        (lambda p: p.cluster_root("NUL"), "algorithm_version"),
        (lambda p: p.cluster_root("com1.txt"), "algorithm_version"),
        (lambda p: p.cluster_root("Lpt9"), "algorithm_version"),
        (lambda p: p.calibration_file("llm", "...", "qs-2026-10-01"), "decider_version"),
        (lambda p: p.cache_partition("qs-2026-10-01", "llm", "a.."), "decider_version"),
    ],
)
def test_ut03_11_invalid_identifiers_rejected(tmp_path: Path, call: object, argument: str) -> None:
    """UT03-11 `laya-../x`, decider `evil` and other bad identifiers raise ConfigError
    naming the argument."""
    paths = _paths(tmp_path)
    assert callable(call)
    with pytest.raises(ConfigError, match=argument):
        call(paths)
