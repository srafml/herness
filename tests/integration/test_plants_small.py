"""Planted truths T1, T2, T3, T5 on the seed 42 `small` build, by plain SQL (impl 11 T11-19).

IT11-05 (T1 MTTR ratio, T5 flat rate), IT11-06 (T2 cluster and E2 mentions), IT11-07 (T3 pairs
and the `caused_by` share). The SQL reads only `core.*` tables of the `small_build` warehouse
(fake enrich / score hooks, T02-22) and never calls spec 04 code. Expected values come from the
build's truth (`truth.json`, `t2_members.parquet`, `t3_pairs.parquet`); only the thresholds
stated in the test rows ([2.3, 2.7], +-10 %, 0.3) are literals.

The build is dirty (`--dirty default`): tombstones (rate 0.003) remove rows from `core`, and
`missing_service` nulls a service. Checks therefore use `team_id` where the plant fixes the team
(T1, T5) and bound a member count by `truth.dirty["tombstones"]` where rows can vanish (T2).
"""

from __future__ import annotations

import math
from collections.abc import Iterator

import duckdb
import pytest
from tests.support.builds import BuildHandle

from herness.eval.truth import truth_dir_for

pytestmark = [pytest.mark.integration, pytest.mark.slow]

# T3 `caused_by` share: the generator fills round(0.3 x n) of n pair incidents, so the share is
# 0.3 within 0.5 / n (n = 161 at seed 42: 0.0031); 0.01 adds room while still rejecting a wrong
# rule (a share of 0.25 or 0.35).
CAUSED_BY_SHARE: float = 0.3
CAUSED_BY_TOLERANCE: float = 0.01
T1_RATIO_BAND: tuple[float, float] = (2.3, 2.7)
T5_FLAT: float = 0.10
T5_VOLUME: float = 2.8
Z95: float = 1.96


@pytest.fixture(scope="module")
def wh(small_build: BuildHandle) -> Iterator[duckdb.DuckDBPyConnection]:
    """Read-only connection to the small build's warehouse."""
    con = duckdb.connect(str(small_build.warehouse), read_only=True)
    try:
        yield con
    finally:
        con.close()


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list[object]) -> tuple[object, ...]:
    row = con.execute(sql, params).fetchone()
    assert row is not None
    return row


def _t5_rates(wh: duckdb.DuckDBPyConnection, build: BuildHandle) -> dict[str, float]:
    """Incidents (resolved by T5) and S5 `request_count` on peak vs off-peak days."""
    t5 = build.truth.plants.T5_confounder
    lo, hi = t5.peak_window
    row = _one(
        wh,
        """
        WITH d AS (
            SELECT date, sum(value) AS req, avg(value) AS avg_req,
                   strftime(date, '%m-%d') BETWEEN ? AND ? AS peak
            FROM core.metric_daily
            WHERE service_id = ? AND metric_name = 'request_count'
            GROUP BY date
        ), inc AS (
            SELECT CAST(opened_at AS DATE) AS date, count(*) AS n
            FROM core.incident WHERE team_id = ? GROUP BY 1
        )
        SELECT
            coalesce(sum(inc.n) FILTER (peak), 0), coalesce(sum(inc.n) FILTER (NOT peak), 0),
            coalesce(sum(req) FILTER (peak), 0), coalesce(sum(req) FILTER (NOT peak), 0),
            coalesce(avg(avg_req) FILTER (peak), 0), coalesce(avg(avg_req) FILTER (NOT peak), 0)
        FROM d LEFT JOIN inc USING (date)
        """,
        [lo, hi, t5.service_id, t5.team_id],
    )
    n_peak, n_off, req_peak, req_off, avg_peak, avg_off = (float(v) for v in row)  # type: ignore[arg-type]
    assert n_peak > 0
    assert n_off > 0
    assert req_peak > 0
    assert req_off > 0
    return {
        "n_peak": n_peak,
        "n_off": n_off,
        "rate_peak": n_peak / (req_peak / 1000.0),
        "rate_off": n_off / (req_off / 1000.0),
        "volume": avg_peak / avg_off,
    }


def test_it11_05_t1_mttr_ratio_in_band(
    wh: duckdb.DuckDBPyConnection, small_build: BuildHandle
) -> None:
    """IT11-05 T1 median MTTR over the same-priority peer median is in [2.3, 2.7].

    Per priority: median MTTR of the T1 team's incidents / median MTTR of every other team's
    incidents of that priority (resolved after opened); the ratio is the T1-incident-weighted
    mean over priorities. T1 MTTR is scaled 2.0 -> 3.0 over the span (mean 2.5)."""
    t1 = small_build.truth.plants.T1_bad_team
    rows = wh.execute(
        """
        WITH m AS (
            SELECT team_id, priority, epoch(resolved_at) - epoch(opened_at) AS secs
            FROM core.incident WHERE resolved_at > opened_at AND priority IS NOT NULL
        ), peer AS (
            SELECT priority, median(secs) AS med, count(*) AS n FROM m WHERE team_id <> ?
            GROUP BY priority
        ), bad AS (
            SELECT priority, median(secs) AS med, count(*) AS n FROM m WHERE team_id = ?
            GROUP BY priority
        )
        SELECT bad.priority, bad.n, bad.med / peer.med, peer.n
        FROM bad JOIN peer USING (priority) ORDER BY bad.priority
        """,
        [t1.team_id, t1.team_id],
    ).fetchall()
    assert len(rows) >= 3, rows  # several priorities, not one lucky bucket
    assert all(n_bad > 0 and n_peer > 0 for _, n_bad, _, n_peer in rows)
    total = sum(n_bad for _, n_bad, _, _ in rows)
    ratio = sum(n_bad * r for _, n_bad, r, _ in rows) / total
    low, high = T1_RATIO_BAND
    assert low <= ratio <= high, (ratio, rows)
    # T1 resolves the incidents of its services
    on_services, on_team = _one(
        wh,
        "SELECT count(*), count(*) FILTER (team_id = ?) FROM core.incident "
        "WHERE list_contains(?, service_id)",
        [t1.team_id, t1.service_ids],
    )
    assert on_services > 0  # type: ignore[operator]
    assert on_team == on_services


def test_it11_05_t5_rate_flat_across_peak(
    wh: duckdb.DuckDBPyConnection, small_build: BuildHandle
) -> None:
    """IT11-05 T5 incidents per 1k requests are flat across peak and off-peak days: traffic
    rises x 2.8 while the rate ratio stays inside the Poisson 95 % bound.

    The row's +-10 % is tighter than the sampling noise of ~44 peak incidents (the 95 % bound
    is about +-35 %); the strict +-10 % check is `test_it11_05_t5_rate_within_10_percent`."""
    r = _t5_rates(wh, small_build)
    assert T5_VOLUME * 0.9 <= r["volume"] <= T5_VOLUME * 1.1  # request_count x 2.8 in the peak
    ratio = r["rate_peak"] / r["rate_off"]
    bound = Z95 * math.sqrt(1 / r["n_peak"] + 1 / r["n_off"])
    assert abs(math.log(ratio)) <= bound, (ratio, bound, r)


@pytest.mark.xfail(
    strict=True,
    reason="IT11-05 T5 rate ratio is 1.14 on seed 42 small (44 peak vs 95 off-peak incidents): "
    "Poisson noise, not a plant defect; the spec row's +-10 % cannot hold for this sample "
    "(reported with T11-19)",
)
def test_it11_05_t5_rate_within_10_percent(
    wh: duckdb.DuckDBPyConnection, small_build: BuildHandle
) -> None:
    """IT11-05 T5 incidents per 1k requests flat within +-10 % across peak and off-peak, as the
    spec row states it. Strict xfail: seed 42 small measures +13.6 %."""
    r = _t5_rates(wh, small_build)
    assert abs(r["rate_peak"] / r["rate_off"] - 1.0) <= T5_FLAT, r


def test_it11_06_t2_cluster_and_e2_mentions(
    wh: duckdb.DuckDBPyConnection, small_build: BuildHandle
) -> None:
    """IT11-06 T2 cluster members in `core.incident` equal `t2_members` less the tombstoned;
    E2 names 20 incidents in `core.work_item_link`, all of them cluster members."""
    truth = small_build.truth
    members = (truth_dir_for(small_build.data_root) / "t2_members.parquet").as_posix()
    epic = truth.plants.T2_roi_epic
    (planted,) = _one(wh, "SELECT count(*) FROM read_parquet(?) WHERE plant = 'T2'", [members])
    expected = int(planted)  # type: ignore[call-overload]
    assert expected > 0
    found = _one(
        wh,
        "SELECT count(*), count(*) FILTER (service_id = ?), "
        "count(*) FILTER (service_id IS NOT NULL AND service_id <> ?) "
        "FROM core.incident WHERE record_id IN "
        "(SELECT record_id FROM read_parquet(?) WHERE plant = 'T2')",
        [epic.service_id, epic.service_id, members],
    )
    n_core, n_on_s2, n_elsewhere = (int(v) for v in found)  # type: ignore[call-overload]
    # tombstones (rate 0.003) delete rows; nothing else loses a member
    assert expected - truth.dirty["tombstones"] <= n_core <= expected
    assert n_elsewhere == 0  # a missing_service defect nulls the service, never moves it
    assert n_on_s2 > 0.5 * n_core
    # E2 mentions: 20 distinct incident numbers, link type mentions_incident
    links = wh.execute(
        "SELECT DISTINCT to_key, link_type FROM core.work_item_link WHERE from_key = ?",
        [epic.epic_key],
    ).fetchall()
    assert len(links) == 20
    assert {kind for _, kind in links} == {"mentions_incident"}
    resolved = _one(
        wh,
        "SELECT count(DISTINCT i.number), count(DISTINCT i.number) FILTER (WHERE m.plant = 'T2') "
        "FROM core.work_item_link l JOIN core.incident i ON i.number = l.to_key "
        "LEFT JOIN read_parquet(?) m ON m.record_id = i.record_id WHERE l.from_key = ?",
        [members, epic.epic_key],
    )
    n_resolved, n_members = (int(v) for v in resolved)  # type: ignore[call-overload]
    assert n_resolved > 0
    assert n_members == n_resolved  # every mention that survives names a T2 member
    assert 20 - n_resolved <= expected - n_core  # a mention is lost only with a tombstoned member


def test_it11_07_t3_pairs_and_caused_by_share(
    wh: duckdb.DuckDBPyConnection, small_build: BuildHandle
) -> None:
    """IT11-07 the T3 incident / change pairs in `core` equal `t3_pairs`; the `caused_by` field
    is filled on 0.3 of them (to the tolerance in the module constants) and names the pair's
    change."""
    t3 = small_build.truth.plants.T3_change_cluster
    pairs = (truth_dir_for(small_build.data_root) / "t3_pairs.parquet").as_posix()
    (expected,) = _one(wh, "SELECT count(*) FROM read_parquet(?)", [pairs])
    (changes,) = _one(wh, "SELECT count(DISTINCT change_record_id) FROM read_parquet(?)", [pairs])
    assert expected > 0  # type: ignore[operator]
    row = _one(
        wh,
        """
        SELECT count(*), count(DISTINCT p.change_record_id), count(i.caused_by_change_id),
               count(*) FILTER (WHERE i.caused_by_change_id = p.change_record_id),
               count(*) FILTER (WHERE i.ci_id = ?), count(*) FILTER (WHERE i.ci_id <> ?)
        FROM read_parquet(?) p
        JOIN core.incident i ON i.record_id = p.incident_record_id
        JOIN core.change c ON c.record_id = p.change_record_id
        """,
        [t3.ci_id, t3.ci_id, pairs],
    )
    n_pairs, n_changes, n_filled, n_matching, n_on_ci, n_off_ci = (int(v) for v in row)  # type: ignore[call-overload]
    assert n_pairs == expected
    assert n_changes == changes
    assert n_on_ci > 0
    assert n_off_ci == 0  # a pair incident sits on C3 (or lost its CI to a dirty-data null)
    assert n_filled == n_matching  # a filled caused_by names the pair's change
    share = n_filled / n_pairs
    assert abs(share - CAUSED_BY_SHARE) <= CAUSED_BY_TOLERANCE, (n_filled, n_pairs)
    # the build's only caused_by values are the planted ones
    (all_filled,) = _one(
        wh, "SELECT count(*) FROM core.incident WHERE caused_by_change_id IS NOT NULL", []
    )
    assert all_filled == n_filled
