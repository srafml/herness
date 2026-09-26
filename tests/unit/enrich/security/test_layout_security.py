"""ST03-10 (TH03-08): path traversal through version strings or configured paths (T03-03)."""

from __future__ import annotations

from pathlib import Path

import pytest

from herness.core.errors import ConfigError
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import DecidersSettings, EmbeddingSettings, LayaSettings

pytestmark = pytest.mark.unit


def _paths(root: Path, *, embedding: str = "data/models/e", current: str = "data/c") -> EnrichPaths:
    return EnrichPaths(
        data_root=root.resolve(), embedding_path=embedding, laya_current_file=current
    )


def test_st03_10_accept_version_traversal_rejected(tmp_path: Path) -> None:
    """ST03-10 `laya accept ../../etc`: the version never reaches the filesystem."""
    with pytest.raises(ConfigError, match="version"):
        _paths(tmp_path).laya_dir("../../etc")


def test_st03_10_configured_path_traversal_rejected(tmp_path: Path) -> None:
    """ST03-10 `embedding.path: ../../x` and a traversing CURRENT file raise ConfigError."""
    root = tmp_path / "root"
    root.mkdir()
    # values come from the real settings models, as a loaded config would carry them
    embedding = EmbeddingSettings(path="../../x").path
    current = DecidersSettings(laya=LayaSettings(current_file="data/../../CURRENT")).laya
    with pytest.raises(ConfigError, match="path outside data root"):
        _paths(root, embedding=embedding).embedding_model_dir()
    with pytest.raises(ConfigError, match="path outside data root"):
        _paths(root, current=current.current_file).laya_current()


def test_st03_10_decider_version_dot_names_rejected(tmp_path: Path) -> None:
    """ST03-10 decider versions `.`/`..` and ids `..` never become path components."""
    paths = _paths(tmp_path)
    for version in (".", ".."):
        with pytest.raises(ConfigError, match="decider_version"):
            paths.calibration_file("openjev", version, "qs-2026-10-01")
    with pytest.raises(ConfigError, match="snapshot_id"):
        paths.cluster_snapshot("v1", "..")


@pytest.mark.parametrize("name", ["...", "a.", "a..", "NUL", "nul.txt", "CON", "aux", "COM1"])
def test_st03_10_windows_aliasing_identifiers_rejected(tmp_path: Path, name: str) -> None:
    """ST03-10 identifiers Win32 would alias (trailing dots) or map to a device are refused."""
    paths = _paths(tmp_path)
    with pytest.raises(ConfigError, match="snapshot_id"):
        paths.cluster_snapshot("v1", name)
    with pytest.raises(ConfigError, match="algorithm_version"):
        paths.cluster_root(name)
    with pytest.raises(ConfigError, match="decider_version"):
        paths.calibration_file("llm", name, "qs-2026-10-01")
