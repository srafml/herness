"""ST03-18 (TH03-16): cluster snapshots are never unpickled (impl 03 U03-103; T03-25).

A pickled `pca.npz` or `prototypes.npy` is refused by `np.load(allow_pickle=False)`, and a
`*.pkl`, `*.bin` or `*.pt` file in a snapshot or version directory refuses the whole load
before anything is read. The canary proves no payload ran: loading with pickle enabled
would call `_Canary.__reduce__`'s target and flip the flag.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.unit.enrich._cluster_support import enrich_paths, tiny_snapshot

from herness.core.errors import ConfigError
from herness.enrich.cluster_stage import ClusterSnapshot

pytestmark = pytest.mark.unit

FIRED: list[str] = []


def _fire(tag: str) -> str:
    FIRED.append(tag)
    return tag


class _Canary:
    def __reduce__(self) -> tuple[object, tuple[str]]:
        return (_fire, ("pickle payload ran",))


def _payload(path: Path) -> None:
    """A numpy pickle of the canary (np.save with pickling on writes one; no `pickle` import)."""
    with path.open("wb") as fh:
        np.save(fh, np.array([_Canary()], dtype=object), allow_pickle=True)


def _saved(tmp_path: Path) -> tuple[ClusterSnapshot, Path]:
    paths = enrich_paths(tmp_path)
    snap = tiny_snapshot()
    snap.save(paths)
    return snap, paths.cluster_snapshot(snap.algorithm_version, snap.snapshot_id)


def test_st03_18_pickled_pca_npz_refused_without_running_payload(tmp_path: Path) -> None:
    """ST03-18 a pca.npz with a pickled object array is refused; the payload never runs."""
    FIRED.clear()
    snap, folder = _saved(tmp_path)
    np.savez(
        folder / "pca.npz",
        components=np.array([_Canary()], dtype=object),
        mean=snap.pca.mean,
        fit_id=np.array(snap.pca.fit_id),
    )
    with pytest.raises(ConfigError, match=r"pca\.npz") as info:
        ClusterSnapshot.load(enrich_paths(tmp_path), snap.algorithm_version, snap.snapshot_id)
    assert FIRED == []
    assert str(folder) not in str(info.value)
    # The threat is real: the same file loaded with pickle enabled runs the payload. Safe
    # here: a test-written file whose only payload is the benign `_fire` canary.
    with np.load(folder / "pca.npz", allow_pickle=True) as z:
        z["components"]
    assert FIRED == ["pickle payload ran"]


def test_st03_18_red_if_loader_allowed_pickle(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST03-18 mutation check: with `np.load(allow_pickle=True)` the payload would run, so
    the `FIRED == []` assertions above fail (the tests are RED for that mutation)."""
    FIRED.clear()
    snap, folder = _saved(tmp_path)
    _payload(folder / "prototypes.npy")
    real_load = np.load

    def pickling_load(*args: Any, **kwargs: Any) -> Any:
        return real_load(*args, **{**kwargs, "allow_pickle": True})

    monkeypatch.setattr(np, "load", pickling_load)
    with contextlib.suppress(ConfigError):
        ClusterSnapshot.load(enrich_paths(tmp_path), snap.algorithm_version, snap.snapshot_id)
    assert FIRED == ["pickle payload ran"]


def test_st03_18_pickled_prototypes_npy_refused(tmp_path: Path) -> None:
    """ST03-18 a prototypes.npy that is a raw pickle is refused; the payload never runs."""
    FIRED.clear()
    snap, folder = _saved(tmp_path)
    _payload(folder / "prototypes.npy")
    with pytest.raises(ConfigError, match=r"prototypes\.npy"):
        ClusterSnapshot.load(enrich_paths(tmp_path), snap.algorithm_version, snap.snapshot_id)
    assert FIRED == []


@pytest.mark.parametrize("name", ["model.pkl", "weights.bin", "state.PT"])
@pytest.mark.parametrize("where", ["snapshot", "version"])
def test_st03_18_pickle_like_files_in_version_dir_refused(
    tmp_path: Path, name: str, where: str
) -> None:
    """ST03-18 `*.pkl`/`*.bin`/`*.pt` in a snapshot or version dir -> ConfigError naming it."""
    FIRED.clear()
    snap, folder = _saved(tmp_path)
    target = folder if where == "snapshot" else folder.parent
    _payload(target / name)
    with pytest.raises(ConfigError, match=name.replace(".", r"\.")) as info:
        ClusterSnapshot.load(enrich_paths(tmp_path), snap.algorithm_version, snap.snapshot_id)
    assert str(target) not in str(info.value)
    assert FIRED == []
