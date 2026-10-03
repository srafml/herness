"""Security test ST07-18 for the outcome job (impl 07 §11.5; U07-87, TH07-18, T07-18).

TH07-18: an outcome verdict must not be skewed by treated peers. Three flat peers are the
honest control; three more peers have their own accepted recommendation on the same metric
in the measured span and improve as much as the target. Were they kept, the peer median would
move with the target and hide its improvement. The guard must also keep peers whose only
recommendation is on another metric, rejected, or effective outside the span, and must never
write an outcome or summary outside its scope (not accepted, no metric).
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    EFFECTIVE,
    accepted_rec,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    run,
    spy_compute,
    summaries,
)

from herness.harness.memory.policy import find_uncited_numerals

pytestmark = pytest.mark.integration

HONEST = ("svc_h1", "svc_h2", "svc_h3")
TREATED = ("svc_x1", "svc_x2", "svc_x3")
KEPT = ("svc_k1", "svc_k2", "svc_k3")


def test_st07_18_peers_with_accepted_recs_on_metric_are_excluded_from_control(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-18 peers with accepted recs on the metric are excluded from control: they are
    never queried or listed, the verdict stays `paid_off`; peers whose recs are on another
    metric, rejected or effective outside the span stay in the control group."""
    del ops_store
    hours = {"svc_t": improves(by=0.4)} | dict.fromkeys(HONEST, flat)
    hours |= dict.fromkeys(TREATED, improves(by=0.4)) | dict.fromkeys(KEPT, flat)
    make_env(tmp_path, monkeypatch, plant(hours))
    calls = spy_compute(monkeypatch)
    for peer in TREATED:
        accepted_rec(peer, effective=EFFECTIVE + timedelta(weeks=1))
    accepted_rec("svc_k1", metric="incident_count")
    accepted_rec("svc_k2", decision="rejected")
    accepted_rec("svc_k3", effective=EFFECTIVE + timedelta(weeks=60))  # after post.end
    rec_id = accepted_rec("svc_t")
    run({"rec_id": rec_id, "measurement": 1})
    [row] = [o for o in outcomes() if o["rec_id"] == rec_id]
    assert row["details"]["method"] == "did_peer_median"
    assert row["details"]["peer_ids"] == sorted((*HONEST, *KEPT))
    assert not set(TREATED) & {e for call in calls for e in call}
    assert row["verdict"] == "paid_off"
    assert row["details"]["rel"] == pytest.approx(0.4, abs=0.02)


def test_st07_18_exclusion_below_min_peers_falls_back_to_prior_year(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-18 when excluding treated peers leaves fewer than `min_peers`, the treated peers
    are still not used: the method falls back to prior_year on the target alone."""
    del ops_store
    hours = {"svc_t": improves(by=0.4)} | dict.fromkeys(HONEST[:2], flat)
    hours |= dict.fromkeys(TREATED, improves(by=0.4))
    env = make_env(tmp_path, monkeypatch, plant(hours))
    calls = spy_compute(monkeypatch)
    for peer in TREATED:
        accepted_rec(peer)
    rec_id = accepted_rec("svc_t")
    run({"rec_id": rec_id, "measurement": 1})
    [row] = [o for o in outcomes() if o["rec_id"] == rec_id]
    assert row["details"]["method"] == "prior_year"
    assert row["details"]["peer_ids"] == []
    assert calls[-2:] == [["svc_t"], ["svc_t"]]
    assert row["verdict"] == "paid_off"  # against its own prior year, not the treated median
    assert env.opened == 1


def test_st07_18_no_outcome_outside_scope_and_no_uncited_numeral(
    ops_store: OpsStoreHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST07-18 a rejected rec, a deferred rec and an accepted rec without a metric get no
    outcome or summary; every written summary passes the system numeral rule (no uncited
    numeral) and cites an evidence query."""
    del ops_store
    hours = {"svc_t": improves(), "svc_r": improves()} | dict.fromkeys(HONEST, flat)
    make_env(tmp_path, monkeypatch, plant(hours))
    rejected = accepted_rec("svc_r", decision="rejected")
    deferred = accepted_rec("svc_r", decision="deferred")
    no_metric = accepted_rec("svc_r", metric=None)
    measured = accepted_rec("svc_t")
    result, _ = run()
    assert result.result["measured"] == 1
    assert {o["rec_id"] for o in outcomes()} == {measured}
    assert {s["data"]["rec_id"] for s in summaries()} == {measured}
    assert not {rejected, deferred, no_metric} & {s["data"]["rec_id"] for s in summaries()}
    for summary in summaries():
        assert find_uncited_numerals(summary["content"], ()) == []
        assert summary["provenance"]["query_ids"]
