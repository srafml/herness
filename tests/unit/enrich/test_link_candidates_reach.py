"""UT03-142: the pruned candidate SQL (T03-38) keeps exactly the U03-108 link set.

``link_candidates`` prunes window matches whose ``opened_at - t_c`` is past the delta at which
the score can still reach ``min_score`` (max boost 1.25 * 1.1) and buckets the range joins by
3 hours. These tests pin the reach boundary and compare the SQL against a brute-force Python
reference of U03-108 built on ``heuristic_link_score`` (U03-107).
"""

from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any

import pytest
from tests.unit.enrich.test_link_changes import T0, Chg, Inc, cand, cfg_with, make_warehouse

from herness.enrich.link_changes import heuristic_link_score, link_candidates

pytestmark = pytest.mark.unit

_MAX_BOOST = 1.25 * 1.1
_OUTCOMES = (None, "successful", "unsuccessful", "backed_out", "successful_with_issues")
_TYPES = (None, "normal", "emergency")


def _us(t: datetime) -> int:
    return (t - T0) // timedelta(microseconds=1)


def _reference(
    incidents: list[Inc], changes: list[Chg], p: dict[str, Any]
) -> dict[tuple[str, str], tuple[str, float]]:
    """U03-108 by brute force over every incident x change pair."""
    ids = {c.record_id for c in changes}
    src = {(i.record_id, i.caused_by) for i in incidents if i.caused_by in ids}
    out: dict[tuple[str, str], tuple[str, float]] = dict.fromkeys(src, ("source_field", 1.0))
    weights = {k: p[k] for k in ("tau_h", "ci_weight", "service_weight")}
    for i in incidents:
        if i.opened_at is None:
            continue
        o = _us(i.opened_at)
        scored: list[tuple[float, str]] = []
        for c in changes:
            t_c = c.actual_end or c.actual_start or c.planned_end
            if t_c is None or (i.record_id, c.record_id) in src:
                continue
            d = o - _us(t_c)
            running = c.actual_start is not None and c.actual_end is not None
            in_run = running and _us(c.actual_start) <= o <= _us(c.actual_end)  # type: ignore[arg-type]
            if not (-p["after_h"] * 3600e6 <= d <= p["before_h"] * 3600e6 or in_run):
                continue
            flags = [(ci, not ci) for ci, a, b in ((True, i.ci_id, c.ci_id),
                     (False, i.service_id, c.service_id)) if a is not None and a == b]  # fmt: skip
            if not flags:
                continue
            score = max(
                heuristic_link_score(same_ci=ci, same_service=svc, delta_h=d / 3600e6,
                                     outcome=c.outcome, change_type=c.type, **weights)
                for ci, svc in flags
            )  # fmt: skip
            if score >= p["min_score"]:
                scored.append((score, c.record_id))
        for score, change in sorted(scored, key=lambda sc: (-sc[0], sc[1]))[: p["top_n"]]:
            out[i.record_id, change] = ("time_ci_window", score)
    return out


def _assert_same(got: dict[tuple[str, str], tuple[str, float]], want: dict[Any, Any]) -> None:
    assert set(got) == set(want)
    for pair, (method, score) in want.items():
        assert got[pair][0] == method
        assert got[pair][1] == pytest.approx(score, rel=1e-12)


# (tag, offset in hours from the analytic reach edge); tags ending "_in" must be kept. The
# 1-minute slack of the SQL prune predicate is 1/60 h: "slack_in" sits at its far end, the
# "past" 36 s offset inside it (dropped by the score filter), "gone" 2 min and "far" 1 h
# beyond the edge (dropped by the prune predicate; a reach tightened by 1 h drops "slack_in").
_REACH_OFFSETS = (
    ("in", -0.01),
    ("slack_in", -1.0 / 60),
    ("past", 0.01),
    ("gone", 2.0 / 60),
    ("far", 1.0),
)


@pytest.mark.parametrize("min_score", [0.30, 0.55])
def test_ut03_142_reach_boundary_keeps_every_link_that_reaches_min_score(min_score: float) -> None:
    """UT03-142 matches inside the reach (0.01 h and 1 min) are kept, past it (36 s inside the
    slack, 2 min and 1 h beyond) dropped."""
    changes = []
    for kind, m, key in (("ci", 1.0, {"ci_id": "ci1"}), ("svc", 0.6, {"service_id": "svc1"})):
        for boosted in (True, False):
            b = _MAX_BOOST if boosted else 1.0
            edge_h = 12.0 * math.log(m * b / min_score)
            extra = {"outcome": "backed_out", "type": "emergency"} if boosted else {}
            for tag, dh in _REACH_OFFSETS:
                end = T0 - timedelta(hours=edge_h + dh)
                changes.append(Chg(f"c_{kind}_{boosted}_{tag}", actual_end=end, **key, **extra))
    incidents = [Inc("i1", ci_id="ci1", service_id="svc1")]
    p = {"before_h": 72.0, "after_h": 1.0, "tau_h": 12.0, "ci_weight": 1.0,
         "service_weight": 0.6, "min_score": min_score, "top_n": 20}  # fmt: skip
    wh = make_warehouse(incidents, changes)
    link_candidates(wh, cfg=cfg_with(min_score=min_score, top_n=20))
    got = cand(wh)
    kept = {c.record_id for c in changes if c.record_id.endswith("_in")}
    assert len(kept) == 8
    assert {c for _, c in got} == kept
    _assert_same(got, _reference(incidents, changes, p))


@pytest.mark.parametrize("min_score", [0.0, 1e-9, 0.9])
def test_ut03_142_window_edges_exact_under_each_prune_regime(min_score: float) -> None:
    """UT03-142 d = -after_h and d = before_h are kept, one second beyond dropped (no pruning,
    reach capped by before_h, negative service reach)."""
    changes = []
    for kind, key in (("ci", {"ci_id": "ci1"}), ("svc", {"service_id": "svc1"})):
        for tag, end in (
            ("after_edge", T0 + timedelta(hours=1)),
            ("after_past", T0 + timedelta(hours=1, seconds=1)),
            ("before_edge", T0 - timedelta(hours=72)),
            ("before_past", T0 - timedelta(hours=72, seconds=1)),
        ):
            changes.append(Chg(f"c_{kind}_{tag}", actual_end=end, **key))
    incidents = [Inc("i1", ci_id="ci1", service_id="svc1")]
    p = {"before_h": 72.0, "after_h": 1.0, "tau_h": 12.0, "ci_weight": 1.0,
         "service_weight": 0.6, "min_score": min_score, "top_n": 20}  # fmt: skip
    wh = make_warehouse(incidents, changes)
    link_candidates(wh, cfg=cfg_with(min_score=min_score, top_n=20))
    got = cand(wh)
    edges = {c.record_id for c in changes if c.record_id.endswith("_edge")}
    # at min_score 0.9 only the CI match at d = -1 h (score 1.0) reaches it
    assert {c for _, c in got} == ({"c_ci_after_edge"} if min_score == 0.9 else edges)
    _assert_same(got, _reference(incidents, changes, p))


_CONFIGS = (
    {},
    {"min_score": 0.0, "top_n": 20},
    {"before_h": 5.0, "after_h": 3.0, "tau_h": 2.0, "ci_weight": 0.0, "service_weight": 1.0,
     "min_score": 0.9, "top_n": 1},
    {"before_h": 200.0, "after_h": 0.5, "tau_h": 48.0, "ci_weight": 0.2, "service_weight": 0.1,
     "min_score": 0.15, "top_n": 5},
    {"min_score": 0.9},  # service reach < 0: 0.6 * 1.375 < 0.9, so no service match survives
)  # fmt: skip


def _random_case(seed: int) -> tuple[list[Inc], list[Chg]]:
    rng = random.Random(seed)
    keys = [None, "k1", "k2", "k3"]

    def at() -> datetime | None:
        return None if rng.random() < 0.1 else T0 + timedelta(minutes=rng.randrange(0, 6000))

    changes = []
    for n in range(40):
        start = at()
        run_h = rng.choice([0.5, 3.0, 30.0, 150.0, -2.0])
        end = None if start is None or rng.random() < 0.2 else start + timedelta(hours=run_h)
        changes.append(Chg(f"c{n:02d}", ci_id=rng.choice(keys), service_id=rng.choice(keys),
                           actual_start=start, actual_end=end, planned_end=at(),
                           outcome=rng.choice(_OUTCOMES), type=rng.choice(_TYPES)))  # fmt: skip
    incidents = [
        Inc(f"i{n:02d}", opened_at=at(), ci_id=rng.choice(keys), service_id=rng.choice(keys),
            caused_by=rng.choice(["c03", "c07", "nope"]) if rng.random() < 0.1 else None)
        for n in range(60)
    ]  # fmt: skip
    return incidents, changes


@pytest.mark.parametrize("seed", [1, 2, 3, 42])
@pytest.mark.parametrize("overrides", _CONFIGS)
def test_ut03_142_candidates_equal_brute_force_reference(seed: int, overrides: dict) -> None:
    """UT03-142 random incidents/changes: link_cand equals the U03-108 brute-force reference."""
    incidents, changes = _random_case(seed)
    cfg = cfg_with(**overrides)
    wh = make_warehouse(incidents, changes)
    link_candidates(wh, cfg=cfg)
    p = cfg.change_link.model_dump()
    _assert_same(cand(wh), _reference(incidents, changes, p))
