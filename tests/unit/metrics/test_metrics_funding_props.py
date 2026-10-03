"""Property test for the funding attribution (impl 04 PT04-05; U04-64; T04-14).

Random link graphs: candidates in a forest (initiatives with epics), incidents with random
services, clusters, costs and direct links, noise events and failed changes, and root-cause
decisions. Cluster-fix thresholds are lowered so pass 2 adds candidates in many examples.
Each example builds its own warehouse with real stage 400 facts (`tests.support.metrics_funding`).
"""

from collections.abc import Mapping, Sequence
from typing import Final

import pytest
from hypothesis import HealthCheck, event, given, settings
from hypothesis import strategies as st
from tests.support.metrics_funding import (
    incident,
    item,
    member,
    mention,
    step_context,
    warehouse,
)
from tests.support.metrics_tiny import tiny_weights

from herness.metrics.funding import ATTRIBUTION_TABLE, run_funding_step
from herness.metrics.settings import WeightsConfig

pytestmark = pytest.mark.unit

# One warehouse and five recorded queries per example: cap the commit profile.
MAX_EXAMPLES: Final = min(settings().max_examples, 25)
SLOW_OK: Final = settings(
    deadline=None, max_examples=MAX_EXAMPLES, suppress_health_check=[HealthCheck.too_slow]
)
SERVICES: Final = ("S1", "S2", "S3")
CLUSTERS: Final = ("C1", "C2")
_EVENT_USD: Final = 5.0  # 3 min / 60 x 100 USD/h
_CHANGE_USD: Final = 400.0  # 4 h x 100 USD/h

type Rows = dict[str, list[dict[str, object]]]


def _weights() -> WeightsConfig:
    w = tiny_weights()
    cf = w.cluster_fix.model_copy(
        update={"min_incidents_12m": 2, "min_annual_pain_usd": 1000, "max_linked_share": 0.5}
    )
    return w.model_copy(update={"cluster_fix": cf})


WEIGHTS: Final = _weights()


@st.composite
def graphs(draw: st.DrawFn) -> Rows:
    """A random funding graph as table rows."""
    n_cand = draw(st.integers(1, 5))
    items: list[dict[str, object]] = []
    for k in range(n_cand):
        parent = draw(st.sampled_from([None, *(f"PAY-{j}" for j in range(k))]))
        kind = draw(st.sampled_from(["initiative", "epic", "feature"]))
        svc = draw(st.sampled_from([None, *SERVICES]))
        items.append(item(f"W{k}", f"PAY-{k}", kind=kind, parent=parent, service=svc))
    stories = draw(st.lists(st.integers(0, n_cand - 1), max_size=2))
    for s, k in enumerate(stories):
        items.append(item(f"ST{s}", f"PAY-S{s}", kind="story", parent=f"PAY-{k}"))
    keys = [str(i["key"]) for i in items]
    n_inc = draw(st.integers(0, 6))
    incidents: list[dict[str, object]] = []
    members: list[dict[str, object]] = []
    links: list[dict[str, object]] = []
    for i in range(n_inc):
        rid = f"I{i}"
        svc = draw(st.sampled_from([None, *SERVICES]))
        minutes = draw(st.integers(0, 600))
        incidents.append(
            incident(rid, service=svc, minutes=minutes, priority=draw(st.integers(1, 4)))
        )
        cluster = draw(st.sampled_from([None, *CLUSTERS]))
        if cluster is not None:
            members.append(member(rid, cluster, draw(st.floats(0.5, 1.0))))
        linked = draw(st.lists(st.sampled_from(keys), max_size=3, unique=True))
        links.extend(mention(key, rid, reverse=draw(st.booleans())) for key in linked)
    ev = {"source_tool": "datadog", "severity": "major", "ts": "2026-02-01 10:00:00-05"}
    events = [
        {**ev, "event_id": f"E{e}", "service_id": draw(st.sampled_from(SERVICES))}
        for e in range(draw(st.integers(0, 2)))
    ]
    ch = {"opened_at": "2026-02-01 08:00:00-05", "actual_end": "2026-02-01 09:00:00-05"}
    changes = [
        {**ch, "record_id": f"CH{c}", "outcome": "unsuccessful",
         "service_id": draw(st.sampled_from(SERVICES))}
        for c in range(draw(st.integers(0, 2)))
    ]  # fmt: skip
    clusters = [
        {
            "cluster_id": c,
            "root_cause_category": "config",
            "service_ids": [draw(st.sampled_from(SERVICES))],
        }
        for c in CLUSTERS
    ]
    decisions = [
        {"record_id": f"W{k}", "question": "root_cause", "answer": "config",
         "probability": draw(st.floats(0.1, 1.0)), "decider": "m1",
         "decided_at": "2026-01-01 00:00:00+00"}
        for k in draw(st.lists(st.integers(0, n_cand - 1), max_size=2, unique=True))
    ]  # fmt: skip
    return {
        "core.work_item": items,
        "core.incident": incidents,
        "enrich.cluster_member": members,
        "core.work_item_link": links,
        "core.event": events,
        "core.change": changes,
        "enrich.cluster": clusters,
        "enrich.decision": decisions,
    }


def _record_usd(con_rows: Sequence[tuple[object, ...]]) -> Mapping[tuple[str, str], float]:
    return {(str(k), str(r)): float(str(u)) for k, r, u in con_rows}


@SLOW_OK
@given(graphs())
def test_pt04_05_shares_and_pain_bounded(rows: Rows) -> None:
    """PT04-05 Σ share ≤ 1 + 1e-9 per record (= 1 when attributed); Σ_k own pain ≤ total + 0.01."""
    con = warehouse(rows, WEIGHTS)
    try:
        run_funding_step(con, step_context(WEIGHTS))
        incident_usd = con.execute(
            "SELECT 'incident', record_id, total_usd FROM metrics.incident_fact"
            " WHERE NOT excluded AND total_usd IS NOT NULL"
        ).fetchall()
        usd = dict(_record_usd(incident_usd))
        usd |= {("event", str(e["event_id"])): _EVENT_USD for e in rows["core.event"]}
        usd |= {("change", str(c["record_id"])): _CHANGE_USD for c in rows["core.change"]}
        stored = con.execute(
            f"SELECT record_kind, record_id, share, pain_usd FROM {ATTRIBUTION_TABLE}"  # noqa: S608
        ).fetchall()
        fix = "starts_with(candidate_id, 'cluster_fix:')"
        paths = con.execute(
            f"SELECT DISTINCT tier, {fix} FROM {ATTRIBUTION_TABLE}"  # noqa: S608 - constants
        ).fetchall()
    finally:
        con.close()
    for tier, fix in paths:  # labels for --hypothesis-show-statistics
        event(f"tier {tier}" + (" cluster_fix" if fix else ""))
    share_sum: dict[tuple[str, str], float] = {}
    own_pain = 0.0
    for kind, record, share, pain_usd in stored:
        key = (str(kind), str(record))
        assert key in usd
        share_sum[key] = share_sum.get(key, 0.0) + float(share)
        exact = float(share) * usd[key]
        own_pain += exact
        # pain_usd is share x usd rounded to cents.
        assert abs(float(pain_usd) - exact) <= 0.005 + 1e-9
    for total_share in share_sum.values():
        assert total_share <= 1 + 1e-9
        assert abs(total_share - 1.0) <= 1e-9
    assert own_pain <= sum(usd.values()) + 0.01
