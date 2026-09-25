"""Tests for herness.eval.truth (U11-28, U11-29).

`tests/support/truth.py` (U11-47, `truth_7_tiny` / `truth_42_tiny`) is built by a much
later card (T11-16) that depends on the full generator pipeline (T11-15, T10-13). This
card (T11-04) is blocked by nothing but T00-03, so these tests build their own minimal
truth directory with `tmp_path` instead of depending on that fixture or on committed
generator output.

The pytest plugin (U11-31) maps each spec test ID to exactly one test function, so
UT11-43 and UT11-44 each cover several scenarios inside a single function.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from herness.core.errors import ConfigError
from herness.eval import truth as t

pytestmark = pytest.mark.unit


def _truth_payload() -> dict[str, Any]:
    return {
        "seed": 42,
        "scale": "tiny",
        "generator_version": "2.0.0",
        "params_hash": "sha256:" + "a" * 64,
        "start": "2023-09-01",
        "end": "2026-08-31",
        "question_set_version": "qs-2026-10-01.1",
        "dataset_root": "data",
        "row_counts": {"servicenow/incident": 100412, "jira/issue": 4120},
        "dirty": {"bad_timestamp": 201, "future_ts": 20},
        "plants": {
            "T1_bad_team": {
                "team_id": "servicenow:sys_user_group:9f1",
                "team_name": "Platform Ops L2",
                "service_ids": ["servicenow:cmdb_ci:1", "servicenow:cmdb_ci:2"],
                "metric": "mttr_hours",
                "lever_usd_model": "mttr",
                "multiplier_start": 2.0,
                "multiplier_end": 3.0,
            },
            "T2_roi_epic": {
                "epic_key": "CHK-1042",
                "epic_record_id": "jira:issue:10442",
                "service_id": "servicenow:cmdb_ci:3",
                "cluster_members_file": "t2_members.parquet",
                "decoy_epic_key": "DATA-310",
            },
            "T2c_cluster_fix": {
                "service_id": "servicenow:cmdb_ci:4",
                "cluster_members_file": "t2_members.parquet",
                "template_id": "tpl_cert_expiry",
            },
            "T3_change_cluster": {
                "ci_id": "servicenow:cmdb_ci:5",
                "owning_team_id": "servicenow:sys_user_group:9f2",
                "pairs_file": "t3_pairs.parquet",
                "source_field_share": 0.3,
            },
            "T4_noisy_service": {
                "service_id": "servicenow:cmdb_ci:6",
                "generated_noise_ratio": 0.953,
            },
            "T5_confounder": {
                "team_id": "servicenow:sys_user_group:9f3",
                "service_id": "servicenow:cmdb_ci:7",
                "peak_window": ["07-15", "08-31"],
                "volume_multiplier": 2.8,
            },
            "T6_outcomes": {
                "paid": {
                    "epic_key": "CHK-2001",
                    "epic_record_id": "jira:issue:20010",
                    "service_id": "servicenow:cmdb_ci:8",
                    "metric": "incident_count",
                    "effect": -0.40,
                },
                "unpaid": {
                    "epic_key": "CHK-2002",
                    "epic_record_id": "jira:issue:20020",
                    "service_id": "servicenow:cmdb_ci:9",
                    "metric": "incident_count",
                    "effect": 0.0,
                },
                "effective_at": "2026-05-01T00:00:00.000000Z",
            },
        },
    }


def _write_truth_dir(root: Path, payload: dict[str, Any] | bytes) -> Path:
    truth_dir = root / "truth"
    truth_dir.mkdir(parents=True)
    data = payload if isinstance(payload, bytes) else json.dumps(payload).encode("utf-8")
    (truth_dir / "truth.json").write_bytes(data)
    return truth_dir


def test_ut11_43_load_truth(tmp_path: Path) -> None:
    """UT11-43 `load_truth` loads a valid manifest; a truncated copy raises `ConfigError`."""
    good_dir = _write_truth_dir(tmp_path / "good", _truth_payload())
    manifest = t.load_truth(good_dir)
    assert manifest.seed == 42
    assert manifest.scale == "tiny"
    assert manifest.start.isoformat() == "2023-09-01"
    assert manifest.plants.T1_bad_team.team_id == "servicenow:sys_user_group:9f1"
    assert manifest.plants.T6_outcomes.paid.effect == -0.40

    full = json.dumps(_truth_payload()).encode("utf-8")
    truncated_dir = _write_truth_dir(tmp_path / "truncated", full[: len(full) // 2])
    with pytest.raises(ConfigError) as truncated_exc:
        t.load_truth(truncated_dir)
    assert str(truncated_dir / "truth.json") == truncated_exc.value.context["path"]

    oversized = b"{" + b" " * (t.MAX_TRUTH_BYTES + 1)
    oversized_dir = _write_truth_dir(tmp_path / "oversized", oversized)
    with pytest.raises(ConfigError):
        t.load_truth(oversized_dir)

    missing_dir = tmp_path / "missing" / "truth"
    missing_dir.mkdir(parents=True)
    with pytest.raises(ConfigError):
        t.load_truth(missing_dir)

    invalid_payload = _truth_payload()
    invalid_payload["seed"] = "not-an-int"
    invalid_dir = _write_truth_dir(tmp_path / "invalid", invalid_payload)
    with pytest.raises(ConfigError) as invalid_exc:
        t.load_truth(invalid_dir)
    assert str(invalid_dir / "truth.json") == invalid_exc.value.context["path"]

    data_root = Path("/srv/herness/build-1/data")
    assert t.truth_dir_for(data_root) == Path("/srv/herness/build-1/truth")


def test_ut11_44_plant_value(tmp_path: Path) -> None:
    """UT11-44 `plant_value` resolves full and short paths; unknown plants raise `SuiteError`."""
    manifest = t.load_truth(_write_truth_dir(tmp_path, _truth_payload()))

    team_id = t.plant_value(manifest, "plants.T1_bad_team.team_id")
    assert team_id == "servicenow:sys_user_group:9f1"

    paid_epic_key = t.plant_value(manifest, "T6.paid.epic_key")
    assert paid_epic_key == "CHK-2001"

    service_ids = t.plant_value(manifest, "plants.T1_bad_team.service_ids")
    assert service_ids == ["servicenow:cmdb_ci:1", "servicenow:cmdb_ci:2"]

    with pytest.raises(t.SuiteError):
        t.plant_value(manifest, "T9.x")

    with pytest.raises(t.SuiteError):
        t.plant_value(manifest, "plants.T1_bad_team.no_such_field")

    with pytest.raises(t.SuiteError):
        t.plant_value(manifest, "plants.T99_nonexistent.team_id")

    with pytest.raises(t.SuiteError):
        t.plant_value(manifest, "not a valid path")

    with pytest.raises(t.SuiteError):
        t.plant_value(manifest, "T6.paid")
