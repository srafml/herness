"""Config benchmarks (impl 10 BT10-01/02). Run: pytest -m "integration and slow" tests/bench."""

from pathlib import Path
from typing import Any

import pytest
from tests.support.config_tree import write_full_config

from herness.core.config import config_hash, load_config

pytestmark = [pytest.mark.integration, pytest.mark.slow]


def _p95(benchmark: Any) -> float:
    data = sorted(benchmark.stats.stats.data)
    return float(data[int(0.95 * (len(data) - 1))])


def test_bt10_01_load_config_p95(benchmark: Any, tmp_path: Path) -> None:
    """BT10-01 load_config over the shipped templates plus owner defaults: p95 under 1 s."""
    cfg_dir = write_full_config(tmp_path)
    benchmark.pedantic(
        load_config, kwargs={"config_dir": cfg_dir, "env": {}}, rounds=20, iterations=1
    )
    assert _p95(benchmark) < 1.0


def test_bt10_02_config_hash_p95(benchmark: Any, tmp_path: Path) -> None:
    """BT10-02 config_hash with key_id given: p95 under 50 ms; stable value across runs."""
    cfg = load_config(config_dir=write_full_config(tmp_path), env={})
    first = config_hash(cfg, key_id="kid_bench")
    result = benchmark.pedantic(
        config_hash, args=(cfg,), kwargs={"key_id": "kid_bench"}, rounds=200, iterations=1
    )
    assert result == first
    assert _p95(benchmark) < 0.050
