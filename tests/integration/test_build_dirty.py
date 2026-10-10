"""Dirty synthetic data through the real pipeline (impl 02 IT02-31; T02-22, T11-19 adds more).

`tools/synth_data.py --seed 42 --scale small --dirty default` is generated once per module,
built with every stage (`build` ... `promote`, fake spec 03 / 04 hooks as in IT02-21) and
checked against `truth/truth.json["dirty"]`, the committed goldens
`tests/fixtures/golden/synth_small_counts.json` / `synth_small_core.json`, and promotion.

Truth key -> `meta.dq_result` check (the generator counts a defect on the record it mutates;
the build sees the deduplicated final rows, so only some keys can be compared exactly):

- `resolved_before_opened`: DQ `resolved_before_opened` (`value` x `rows`), within +-1.
- `unknown_enum`: `cast_fail:stg.sn_incident.priority` + `cast_fail:stg.sn_change_request.type`
  (failed rows), within +-1.
- `missing_service`: `incident_service_null` (`null_rows`), truth - tombstones <= dq <= truth.
- `bad_timestamp`: `cast_fail` of the incident and change timestamp columns,
  truth / 2 <= dq <= truth.
- `future_ts`: `future_timestamp:core.incident.opened_at`, 0 <= dq <= truth.
- `duplicate_rows`, `later_versions`, `tombstones`: no DQ counterpart (staging dedupe removes or
  replaces them); covered by `duplicate_key:*` = 0 and the goldens' row counts.

Why the bounds: a tombstone removes the incident from `core` after the defect was counted;
one third of `bad_timestamp` writes an empty value (NULL, not a failed cast); `future_ts`
moves lifecycles by 730 days, so how many rows lie after the build start depends on the
date the test runs (the golden leaves `future_timestamp:*` out for that reason).

T11-19 adds IT11-08 on the shared `small_build` (same seed 42 small, `--dirty default`): the
T4 alert-noise plant, by plain SQL on `core.event` with the spec 04 `alert_noise_ratio`
definition (noise-severity events with no incident / all noise-severity events; severities and
`min_sample_size` read from `config/metrics.yaml`, no `herness.metrics` import). DD11-05 (event
shares of the noise plant) is open; the controller ruled to proceed with the generator default
0.50 / 0.90 `near_incident_share` / `near_incident_ref_share` and the card's acceptance text.
"""

from __future__ import annotations

import json
from typing import Any

import duckdb
import pytest
import yaml
from tests.support.builds import REPO, BuildHandle
from tests.support.synth_build import (
    GOLDEN_CORE,
    GOLDEN_COUNTS,
    SynthBuild,
    build_synth,
    generate,
    golden_core,
    golden_counts,
)

pytestmark = [pytest.mark.integration, pytest.mark.slow]

_INCIDENT_TS = ("opened_at", "acknowledged_at", "resolved_at", "closed_at")
_CHANGE_TS = ("opened_at", "planned_start", "planned_end", "actual_start", "actual_end")


@pytest.fixture(scope="module")
def synth(tmp_path_factory: pytest.TempPathFactory) -> SynthBuild:
    """Seed 42 `small` dirty `default`, generated into a temp root and fully built."""
    base = tmp_path_factory.mktemp("synth_small42")
    root = base / "root"
    generate(root)
    return build_synth(root, base / "cfg")


def _failed(dq: dict[str, Any], names: list[str]) -> int:
    return int(sum(dq[f"cast_fail:{n}"]["details"]["failed"] for n in names))


def test_it02_31_promoted_with_clean_keys(synth: SynthBuild) -> None:
    """IT02-31 the dirty build passes the DQ gate and is promoted; staging dedupe leaves no
    duplicate key in any core table."""
    assert synth.promoted
    assert synth.result["stages"] == ["build", "enrich", "score", "dq", "promote"]
    assert synth.result["dq"]["failed_errors"] == []
    keys = {k: v for k, v in synth.dq.items() if k.startswith("duplicate_key:")}
    assert keys
    assert {k: v["value"] for k, v in keys.items() if v["value"]} == {}


def test_it02_31_dq_counts_match_truth(synth: SynthBuild) -> None:
    """IT02-31 DQ defect counts within +-1 of `truth.dirty` where the build can observe the
    defect exactly; bounded (see the module docstring) where tombstones, NULL writes or the
    clock hide part of it."""
    truth = synth.truth["dirty"]
    dq = synth.dq
    # observable exactly (+-1)
    rbo = dq["resolved_before_opened"]
    assert abs(round(rbo["value"] * rbo["details"]["rows"]) - truth["resolved_before_opened"]) <= 1
    enum = _failed(dq, ["stg.sn_incident.priority", "stg.sn_change_request.type"])
    assert abs(enum - truth["unknown_enum"]) <= 1
    # bounded by tombstones, NULL writes and the clock
    missing = dq["incident_service_null"]["details"]["null_rows"]
    assert truth["missing_service"] - truth["tombstones"] <= missing <= truth["missing_service"]
    names = [f"stg.sn_incident.{c}" for c in _INCIDENT_TS]
    names += [f"stg.sn_change_request.{c}" for c in _CHANGE_TS]
    bad_ts = _failed(dq, names)
    assert truth["bad_timestamp"] / 2 <= bad_ts <= truth["bad_timestamp"]
    future = dq["future_timestamp:core.incident.opened_at"]["value"]
    assert 0 <= future <= truth["future_ts"]


def test_it02_31_matches_goldens(synth: SynthBuild) -> None:
    """IT02-31 `core.*` row counts, DQ counts and the sorted-row digests equal the committed
    goldens (`synth_small_counts.json`, `synth_small_core.json`)."""
    assert golden_counts(synth) == json.loads(GOLDEN_COUNTS.read_text(encoding="utf-8"))
    assert golden_core(synth) == json.loads(GOLDEN_CORE.read_text(encoding="utf-8"))
    for table, rows in json.loads(GOLDEN_COUNTS.read_text(encoding="utf-8"))["core"].items():
        assert synth.row_counts[table] == rows


# --- IT11-08 (T11-19): T4 alert-noise plant on the small build -----------------------------

NOISE_PLANT_MIN: float = 0.9
NOISE_OTHERS_MAX: float = 0.6


def _noise_definition() -> tuple[list[str], int]:
    """`noise_severities` and the metric's `min_sample_size` from the shipped metric catalog."""
    cfg = yaml.safe_load((REPO / "config" / "metrics.yaml").read_text(encoding="utf-8"))
    entry = next(m for m in cfg["metrics"] if m["name"] == "alert_noise_ratio")
    return list(cfg["defaults"]["noise_severities"]), int(entry["min_sample_size"])


@pytest.fixture(scope="module")
def noise_by_service(small_build: BuildHandle) -> dict[str, tuple[float, int]]:
    """service_id -> (alert_noise_ratio, noise-severity event count) over `core.event`."""
    severities, _ = _noise_definition()
    con = duckdb.connect(str(small_build.warehouse), read_only=True)
    try:
        rows = con.execute(
            "SELECT service_id, count(*) FILTER (WHERE incident_id IS NULL) * 1.0 / count(*), "
            "count(*) FROM core.event WHERE service_id IS NOT NULL "
            "AND list_contains(?, severity) GROUP BY service_id",
            [severities],
        ).fetchall()
    finally:
        con.close()
    return {str(sid): (float(ratio), int(n)) for sid, ratio, n in rows}


def test_it11_08_t4_service_noise_first_and_high(
    small_build: BuildHandle, noise_by_service: dict[str, tuple[float, int]]
) -> None:
    """IT11-08 S4 `alert_noise_ratio` >= 0.9 and first among all services (also among those
    meeting the metric's minimum sample); all other noise events pooled stay <= 0.6."""
    s4 = small_build.truth.plants.T4_noisy_service
    _, min_n = _noise_definition()
    assert noise_by_service
    ratio, n = noise_by_service[s4.service_id]
    assert n >= min_n
    assert ratio >= NOISE_PLANT_MIN
    assert ratio == pytest.approx(s4.generated_noise_ratio, abs=0.02)
    others = {k: v for k, v in noise_by_service.items() if k != s4.service_id}
    assert others
    assert all(ratio > r for r, _ in others.values())  # first, with no tie
    pooled_events = sum(n_o for _, n_o in others.values())
    pooled_noise = sum(r_o * n_o for r_o, n_o in others.values()) / pooled_events
    assert pooled_noise <= NOISE_OTHERS_MAX, pooled_noise


@pytest.mark.xfail(
    strict=True,
    reason="IT11-08 open DD11-05: at near_incident_share 0.50 / ref share 0.90 the background "
    "noise ratio is ~0.57 on average and 37 of 124 services with >= 50 noise events exceed "
    "0.6 (max 0.78) on seed 42 small; binomial spread, reported with T11-19",
)
def test_it11_08_every_other_service_at_most_0_6(
    small_build: BuildHandle, noise_by_service: dict[str, tuple[float, int]]
) -> None:
    """IT11-08 every other service with the metric's minimum sample has `alert_noise_ratio`
    <= 0.6, as the spec row states it. Strict xfail: seed 42 small measures up to 0.78."""
    s4 = small_build.truth.plants.T4_noisy_service
    _, min_n = _noise_definition()
    scored = {k: r for k, (r, n) in noise_by_service.items() if k != s4.service_id and n >= min_n}
    assert scored
    assert max(scored.values()) <= NOISE_OTHERS_MAX, max(scored.values())
