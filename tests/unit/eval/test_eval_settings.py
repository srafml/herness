"""Tests for herness.eval.settings: config/eval.yaml models (T11-20).

UT11-107 loads the committed `config/eval.yaml`, checks it equals design 11 §7 field for
field, and checks a `repeat` bound violation is rejected. UT11-108 checks an invalid
`correctness_floor` key and a non-positive ratio field are both rejected.
"""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import pytest
import yaml

from herness.core.errors import ConfigError
from herness.eval import settings as s

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
CONFIG_PATH = ROOT / "config" / "eval.yaml"


def _raw() -> dict[str, Any]:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


def test_ut11_107_config_eval_yaml_matches_design() -> None:
    """UT11-107 config/eval.yaml loads and equals design 11 §7 field for field."""
    raw = _raw()
    cfg = s.load_eval_config(raw)

    assert cfg.suite == "tests/eval/golden.yaml"
    assert cfg.judge == s.JudgeSettings(
        profile="local-judge", temperature=0, cache_dir="data/cache/judge"
    )
    assert cfg.repeat == {"fast": 1, "standard": 1, "deep": 3}
    assert cfg.thresholds == s.Thresholds(
        unsupported_number_rate_max=0,
        correctness_drop_max_pp=3,
        correctness_floor={
            "local-standard": 0.80,
            "local-deep": 0.85,
            "hybrid": 0.88,
            "premium": 0.90,
        },
        tool_success_min={"local": 0.90, "hybrid": 0.95, "premium": 0.95},
        latency_p95_ratio_max=1.25,
        cost_ratio_max=1.20,
        bench_regression_max=0.20,
    )
    assert cfg.classifier == s.ClassifierSettings(
        gate_recompute_tolerance=0.005, synthetic_sample=5000, bootstrap=1000
    )
    assert cfg.baseline == "synthetic-42-small"


def test_ut11_107_repeat_bound_rejected() -> None:
    """UT11-107 repeat.deep = 11 (outside 1..10) raises ConfigError."""
    raw = copy.deepcopy(_raw())
    raw["repeat"]["deep"] = 11
    with pytest.raises(ConfigError):
        s.load_eval_config(raw)


def test_ut11_108_invalid_correctness_floor_key_rejected() -> None:
    """UT11-108 an unknown correctness_floor key raises ConfigError."""
    raw = copy.deepcopy(_raw())
    raw["thresholds"]["correctness_floor"]["bogus-profile"] = 0.5
    with pytest.raises(ConfigError):
        s.load_eval_config(raw)


def test_ut11_108_non_positive_ratio_rejected() -> None:
    """UT11-108 latency_p95_ratio_max = 0 (must be > 0) raises ConfigError."""
    raw = copy.deepcopy(_raw())
    raw["thresholds"]["latency_p95_ratio_max"] = 0
    with pytest.raises(ConfigError):
        s.load_eval_config(raw)
