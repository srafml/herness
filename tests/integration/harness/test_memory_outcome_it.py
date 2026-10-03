"""Integration test IT07-05 for the outcome job (impl 07 F07-08; U07-86, U07-87, T07-18).

Spec 11's `tiny_build` is not in the tree: `_outcome_env.plant` builds the warehouse with the
spec 02 DDL and planted weekly incidents, materialized by the real spec 04 facts step; the
handler runs the real `compute_metric`, `peer_group`, ops closed-loop functions and writer.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    accepted_rec,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    run,
    summaries,
)

from herness.harness.memory.policy import find_uncited_numerals

pytestmark = pytest.mark.integration

PEERS = ("svc_p1", "svc_p2", "svc_p3")


def test_it07_05_planted_improvement_paid_off_then_no_effect_then_rerun_is_noop(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-05 a planted 20 % MTTR improvement against flat peers is `paid_off`; the same
    improvement in every peer is `no_effect`; a rerun of the sweep writes no new row."""
    del ops_store
    con = plant({"svc_t": improves()} | dict.fromkeys(PEERS, flat))
    make_env(tmp_path, monkeypatch, con)
    paid = accepted_rec("svc_t")
    result, _ = run()
    assert result.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    [row] = outcomes()
    assert (row["rec_id"], row["verdict"]) == (paid, "paid_off")
    assert row["details"]["method"] == "did_peer_median"
    assert row["details"]["rel"] == pytest.approx(0.2, abs=0.01)

    rerun, _ = run()
    assert rerun.result == {"measured": 0, "skipped": 0, "verdicts": {}}
    assert len(outcomes()) == 1
    assert len(summaries()) == 1


def test_it07_05_same_improvement_in_all_peers_is_no_effect(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-05 when every peer improves by the same 20 % the peer-adjusted effect is zero:
    `no_effect`; rerunning the single-pair job writes no new row."""
    del ops_store
    con = plant({"svc_t": improves()} | dict.fromkeys(PEERS, improves()))
    make_env(tmp_path, monkeypatch, con)
    rec_id = accepted_rec("svc_t")
    result, _ = run({"rec_id": rec_id, "measurement": 1})
    assert result.result == {"measured": 1, "skipped": 0, "verdicts": {"no_effect": 1}}
    again, _ = run({"rec_id": rec_id, "measurement": 1})
    assert again.result == {"skipped": "not_due"}
    [row] = outcomes()
    assert row["verdict"] == "no_effect"
    [summary] = summaries()
    assert summary["content"].endswith("showed no measurable effect (first measurement).")
    assert find_uncited_numerals(summary["content"], ()) == []
