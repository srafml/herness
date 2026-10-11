"""Tests for the per-process catalog memo (impl 04 UT04-124, T04-24)."""

import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import yaml
from tests.support.metrics_render import ROOT, metric, weights

from herness.core.errors import ConfigError
from herness.metrics import catalog as cat
from herness.metrics._catalog_cache import reset_catalog_cache
from herness.metrics.catalog import load_catalog

pytestmark = pytest.mark.unit

SHIPPED = ROOT / "config" / "metrics.yaml"


def _raw(min_sample_size: int | None = None) -> dict[str, Any]:
    """The shipped file reduced to the one `mttr_hours` fixture metric."""
    raw: dict[str, Any] = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))
    entry = metric()
    if min_sample_size is not None:
        entry = entry.model_copy(update={"min_sample_size": min_sample_size})
    raw["metrics"] = [entry.model_dump(mode="json")]
    raw["scoring"]["org"]["metrics"] = {"mttr_hours": 1.0}
    return raw


@pytest.fixture
def catalog_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A one-metric catalog file with `get_config()` reduced to the shipped weights."""
    monkeypatch.setattr(cat, "get_config", lambda: SimpleNamespace(weights=weights()))
    reset_catalog_cache()
    path = tmp_path / "metrics.yaml"
    path.write_text(yaml.safe_dump(_raw()), encoding="utf-8")
    return path


def test_ut04_124_second_load_is_cached_and_identical(catalog_file: Path) -> None:
    """UT04-124 the second load in a process is under 50 ms and returns the same object."""
    first = load_catalog(catalog_file)
    start = time.perf_counter()
    second = load_catalog(catalog_file)
    assert time.perf_counter() - start < 0.05
    assert second is first


def test_ut04_124_file_edit_reloads(catalog_file: Path) -> None:
    """UT04-124 a rewritten catalog file (new digest) is reloaded, never served stale."""
    first = load_catalog(catalog_file)
    catalog_file.write_text(yaml.safe_dump(_raw(min_sample_size=7)), encoding="utf-8")
    second = load_catalog(catalog_file)
    assert second is not first
    assert second.get("mttr_hours").min_sample_size == 7


def test_ut04_124_weights_change_reloads(
    catalog_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT04-124 a different weights block (the config input of the key) reloads."""
    first = load_catalog(catalog_file)
    block = next(iter(weights().blocks()))
    changed = weights(**{block: not getattr(weights(), block).unconfirmed})
    monkeypatch.setattr(cat, "get_config", lambda: SimpleNamespace(weights=changed))
    assert load_catalog(catalog_file) is not first


def test_ut04_124_failure_raises_every_call_and_is_not_cached(catalog_file: Path) -> None:
    """UT04-124 a failing catalog raises ConfigError on every call; nothing is cached."""
    good = load_catalog(catalog_file)
    catalog_file.write_text("a: [unclosed", encoding="utf-8")
    for _ in range(2):
        with pytest.raises(ConfigError):
            load_catalog(catalog_file)
    catalog_file.write_text(yaml.safe_dump(_raw()), encoding="utf-8")
    assert load_catalog(catalog_file) is good  # failures neither poisoned nor evicted the memo


def test_ut04_124_concurrent_loads_share_one_object(catalog_file: Path) -> None:
    """UT04-124 eight threads loading at once receive one object."""
    results: list[object] = []
    barrier = threading.Barrier(8)

    def work() -> None:
        barrier.wait()
        results.append(load_catalog(catalog_file))

    threads = [threading.Thread(target=work) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 8
    assert all(r is results[0] for r in results)
