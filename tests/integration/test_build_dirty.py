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
"""

from __future__ import annotations

import json
from typing import Any

import pytest
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
