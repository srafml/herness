"""Seed-derived catalog: orgs, teams, services, CIs, projects, change schedule, plants (U11-04).

Built once in the parent and passed read-only to the shard workers (design §5.1.8). Rows
come from `stream_rng(seed, "catalog:<class>")` and plant choices from
`stream_rng(seed, "plants:<class>")`, so `small` and `full` share one catalog. Names and
costs come from `tools.synth.catalog_pools`; plant names are drawn like every other name.
"""

import dataclasses
import math
from collections.abc import Sequence
from datetime import UTC, date, datetime, time, timedelta
from typing import Final, NoReturn

import numpy as np
import numpy.typing as npt

from tools.synth import catalog_pools as pools
from tools.synth.catalog_rows import (
    Catalog,
    ChangeSlot,
    CiRow,
    MonthKey,
    OrgRow,
    Planner,
    PlantTargets,
    ProjectRow,
    RelRow,
    ServiceRow,
    TeamRow,
)
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.rng import STREAM_CATALOG, STREAM_PLANTS, stream_rng

Floats = npt.NDArray[np.float64]
Ints = npt.NDArray[np.int64]

_LEVELS: Final = 3  # team name suffix L1..L3
_DEPENDS: Final = "Depends on::Used by"
_RUNS: Final = "Runs on::Runs"
_SHARE_TOLERANCE: Final = 0.02  # top-5 % share accepted in target +/- this
_T6_SHARE: Final = {"full": 0.015, "small": 0.05, "tiny": 0.05}
_T6_MIN_PEERS: Final = 5
_T3_CHANGES: Final = 40  # emergency and, again, normal (control) changes on C3
_METRICS_PER_SERVICE: Final = 4


@dataclasses.dataclass(slots=True)
class _Draft:
    """Mutable service columns while plants adjust weights and support teams."""

    ids: list[str]
    names: list[str]
    crit: Ints
    owner: Ints
    support: Ints
    team_p: Floats
    inc: Floats
    evt: Floats
    run_cost: Floats
    downtime: Floats


def _hex_id(rng: np.random.Generator) -> str:
    return rng.bytes(16).hex()


def _fail(what: str) -> NoReturn:
    msg = f"preset cannot hold plant {what}"
    raise SynthUsageError(msg, key="preset")


def _split(total: int, weights: Sequence[float]) -> list[int]:
    """Split `total` proportionally to `weights` with largest-remainder rounding."""
    quotas = [total * w / math.fsum(weights) for w in weights]
    counts = [math.floor(q + 1e-9) for q in quotas]
    order = sorted(range(len(quotas)), key=lambda i: (counts[i] - quotas[i], i))
    for i in order[: total - sum(counts)]:
        counts[i] += 1
    return counts


def _team_counts(rng: np.random.Generator, params: SynthParams) -> list[int]:
    preset, org = params.preset, params.org
    if preset.catalog_class == "tiny":
        return _split(preset.teams, [1.0] * preset.orgs)
    counts = rng.poisson(org.teams_per_org_mean, preset.orgs)
    counts = np.clip(counts, org.teams_per_org_min, org.teams_per_org_max)
    while (gap := preset.teams - int(counts.sum())) != 0:
        counts[int(np.argmax(counts))] += 1 if gap > 0 else -1
    return [int(c) for c in counts]


def _teams(rng: np.random.Generator, params: SynthParams, orgs: list[OrgRow]) -> list[TeamRow]:
    homes = [o for o, n in zip(orgs, _team_counts(rng, params), strict=True) for _ in range(n)]
    total = len(homes)
    combos = rng.choice(
        len(pools.AREAS) * len(pools.FUNCTIONS) * _LEVELS, size=total, replace=False
    )
    multipliers = np.exp(rng.normal(0.0, params.mttr.team_multiplier_sigma, total))
    managers = rng.integers(0, len(pools.FIRST_NAMES), (total, 2))
    rows = []
    for i, org in enumerate(homes):
        area, rest = divmod(int(combos[i]), len(pools.FUNCTIONS) * _LEVELS)
        name = f"{pools.AREAS[area]} {pools.FUNCTIONS[rest // _LEVELS]} L{rest % _LEVELS + 1}"
        manager = f"{pools.FIRST_NAMES[managers[i, 0]]} {pools.LAST_NAMES[managers[i, 1]]}"
        mult = float(multipliers[i])
        extra = params.reassign.extra_mean if mult > params.reassign.extra_threshold else 0.0
        rows.append(TeamRow(_hex_id(rng), name, org.sys_id, manager, org.cost_center, mult, extra))
    return rows


def _services(rng: np.random.Generator, params: SynthParams, n_teams: int) -> _Draft:
    n, levels = params.preset.services, sorted(params.org.criticality)
    combos = rng.choice(len(pools.ADJECTIVES) * len(pools.NOUNS), size=n, replace=False)
    names = [
        f"{pools.ADJECTIVES[c // len(pools.NOUNS)]} {pools.NOUNS[c % len(pools.NOUNS)]}"
        for c in combos
    ]
    quota = _split(n, [params.org.criticality[c] for c in levels])  # exact mix, shuffled
    crit = rng.permutation(np.repeat(np.array(levels, dtype=np.int64), quota))
    team_w = rng.pareto(1.5, n_teams) + 1.0
    team_p = team_w / team_w.sum()
    owner, support = rng.choice(n_teams, size=n, p=team_p), rng.choice(n_teams, size=n, p=team_p)
    alpha, boost = params.incident.pareto_alpha, params.incident.criticality1_weight
    inc = (rng.pareto(alpha, n) + 1.0) * np.where(crit == 1, boost, 1.0)
    evt = rng.pareto(alpha, n) + 1.0
    scale = np.exp(rng.normal(0.0, 0.4, (2, n)))
    run = np.array([pools.RUN_COST_USD[int(c)] for c in crit]) * scale[0]
    down = np.array([pools.DOWNTIME_COST_USD[int(c)] for c in crit]) * scale[1]
    ids = [_hex_id(rng) for _ in range(n)]
    return _Draft(ids, names, crit, owner, support, team_p, inc, evt, run, down)


def _cis(rng: np.random.Generator, d: _Draft) -> tuple[list[CiRow], list[RelRow], list[str]]:
    cis: list[CiRow] = []
    rels: list[RelRow] = []
    appls: list[str] = []
    for sid, name, crit in zip(d.ids, d.names, d.crit, strict=True):
        slug = name.lower().replace(" ", "-")
        appl = CiRow(_hex_id(rng), f"{name} App", "cmdb_ci_appl", sid)
        cis += [CiRow(sid, name, "cmdb_ci_service", sid), appl]
        rels.append(RelRow(_hex_id(rng), sid, appl.sys_id, _DEPENDS))
        appls.append(appl.sys_id)
        for k in range(int(rng.integers(1, 4))):
            cis.append(CiRow(_hex_id(rng), f"{slug}-srv{k + 1:02d}", "cmdb_ci_server", sid))
            rels.append(RelRow(_hex_id(rng), appl.sys_id, cis[-1].sys_id, _RUNS))
        if crit <= 2:  # noqa: PLR2004 - criticality 1 and 2 get a database
            cis.append(CiRow(_hex_id(rng), f"{slug}-db", "cmdb_ci_db_instance", sid))
            rels.append(RelRow(_hex_id(rng), appl.sys_id, cis[-1].sys_id, _DEPENDS))
    return cis, rels, appls


def _projects(orgs: list[OrgRow]) -> list[ProjectRow]:
    rows: list[ProjectRow] = []
    for i, org in enumerate(orgs):
        key, suffix = org.name[:4].upper(), 2
        while key in {r.key for r in rows}:
            key, suffix = f"{org.name[:3].upper()}{suffix}", suffix + 1
        rows.append(ProjectRow(key, org.name, 10_000 * (i + 1)))
    return rows


def _with_s6(w: Floats, s6: list[int], frac: float) -> Floats:
    """Set each S6 weight so it carries `frac` of the total (U11-04 step 9)."""
    w[s6] = frac * (w.sum() - w[s6].sum()) / (1.0 - 2.0 * frac)
    return w


def _calibrate(d: _Draft, s6: list[int], t1: list[int], params: SynthParams) -> None:
    """Tail calibration (step 5) with the S6 overrides of step 9 applied at every gamma;
    T1 weights are then clamped to +/-20 % of the final median."""
    frac = _T6_SHARE[params.preset.name]
    k = max(1, round(len(d.inc) * params.incident.top_service_share))
    target = params.incident.top_share_target

    def share(gamma: float) -> float:
        w = _with_s6(d.inc**gamma, s6, frac)
        return float(np.sort(w)[-k:].sum() / w.sum())

    gamma = 1.0
    if abs(share(1.0) - target) > _SHARE_TOLERANCE:
        low, high = 0.5, 3.0
        for _ in range(40):
            mid = (low + high) / 2.0
            low, high = (mid, high) if share(mid) < target else (low, mid)
        gamma = (low + high) / 2.0
    w = _with_s6(d.inc**gamma, s6, frac)
    median = float(np.median(w))
    w[t1] = np.clip(w[t1], 0.8 * median, 1.2 * median)
    d.inc = _with_s6(w, s6, frac)


class _Picker:
    """Draws plant services from `rng_p`; each service serves at most one plant."""

    def __init__(self, rng: np.random.Generator, d: _Draft) -> None:
        self.rng, self.d, self.used = rng, d, set[int]()

    def free(self, *crits: int) -> list[int]:
        return [i for i, c in enumerate(self.d.crit) if i not in self.used and int(c) in crits]

    def pick(self, what: str, *crits: int) -> int:
        cands = self.free(*crits)
        if not cands:
            _fail(what)
        self.used.add(chosen := cands[int(self.rng.integers(len(cands)))])
        return chosen

    def pick_t1(self) -> list[int]:
        """Three criticality-2 services within +/-20 % of the median base weight, else the
        three closest; `_calibrate` clamps them into the band of the final weights."""
        cands, median = self.free(2), float(np.median(self.d.inc))
        if len(cands) < 3:  # noqa: PLR2004 - T1 has three services
            _fail("T1")
        near = [i for i in cands if abs(self.d.inc[i] - median) <= 0.2 * median]
        if len(near) >= 3:  # noqa: PLR2004
            chosen = sorted(int(i) for i in self.rng.choice(near, size=3, replace=False))
        else:
            chosen = sorted(cands, key=lambda i: (abs(self.d.inc[i] - median), i))[:3]
        self.used.update(chosen)
        return chosen


def _assign_plant_teams(rng: np.random.Generator, d: _Draft, plan: dict[int, list[int]]) -> None:
    """Make each plant team the only support team of exactly its services."""
    served = {s for services in plan.values() for s in services}
    others = np.array([t for t in range(len(d.team_p)) if t not in plan], dtype=np.int64)
    p = d.team_p[others] / d.team_p[others].sum()
    for i in range(len(d.ids)):
        if i not in served and int(d.support[i]) in plan:
            d.support[i] = others[int(rng.choice(len(others), p=p))]
    for team, services in plan.items():
        d.support[services] = team


def _plants(
    rng: np.random.Generator, d: _Draft, params: SynthParams, teams: list[str], appls: list[str]
) -> tuple[PlantTargets, int, int]:
    """Choose the plant targets (step 8), apply weight overrides (step 9); also return the
    indices of teams T1 and T5."""
    pick = _Picker(rng, d)
    s6p, s6u = pick.pick("T6", 3), pick.pick("T6", 3)
    if len(peers := pick.free(3)) < _T6_MIN_PEERS:
        _fail("T6 (fewer than 7 criticality-3 services)")
    s3, s2 = pick.pick("T3", 2), pick.pick("T2", 1)
    t1_services = pick.pick_t1()  # on base weights, so small and full agree
    _calibrate(d, [s6p, s6u], t1_services, params)
    s2c = pick.pick("T2c", 1, 2)
    s4, s5 = pick.pick("T4", 1, 2, 4), pick.pick("T5", 1, 2, 4)
    if not (decoys := pick.free(4)):
        _fail("T2 decoy")
    d.evt[s4] = 0.0
    t1, t5 = (int(t) for t in rng.choice(len(d.team_p), size=2, replace=False))
    _assign_plant_teams(rng, d, {t1: t1_services, t5: [s5]})
    e2d = min(decoys, key=lambda i: (float(d.inc[i]), i))
    effective_at, peak = _windows(params)
    ids = d.ids
    targets = PlantTargets(
        teams[t1], tuple(ids[i] for i in t1_services), ids[s2], ids[s2c], ids[e2d], ids[s3],
        appls[s3], ids[s4], teams[t5], ids[s5], ids[s6p], ids[s6u],
        tuple(ids[i] for i in peers), effective_at, peak,
    )  # fmt: skip
    return targets, t1, t5


def _windows(params: SynthParams) -> tuple[datetime, tuple[str, str]]:
    end = params.end
    if params.preset.catalog_class == "tiny":
        start = end - timedelta(days=29)
        day, window = end - timedelta(days=42), (start.strftime("%m-%d"), end.strftime("%m-%d"))
    else:
        months = end.year * 12 + end.month - 1 - 3
        day, window = date(months // 12, months % 12 + 1, 1), ("07-15", "08-31")
    return datetime.combine(day, time(), UTC), window


def _change_schedule(seed: int, params: SynthParams) -> tuple[ChangeSlot, ...]:
    rng = stream_rng(seed, STREAM_PLANTS + ":t3")
    first = datetime.combine(params.start, time(), UTC)
    last = datetime.combine(params.end + timedelta(days=1), time(), UTC) - timedelta(hours=2)
    span = (last - first).total_seconds()
    slots: list[tuple[datetime, str, bool, int]] = []
    for emergency in (True, False):
        offsets = (np.arange(_T3_CHANGES) + rng.random(_T3_CHANGES)) / _T3_CHANGES * span
        follow = rng.integers(2, 7, _T3_CHANGES) if emergency else np.zeros(_T3_CHANGES, int)
        for offset, k in zip(offsets, follow, strict=True):
            at = first + timedelta(seconds=int(offset))
            slots.append((at, _hex_id(rng), emergency, int(k)))
    slots.sort()
    return tuple(ChangeSlot(sid, i, at, em, k) for i, (at, sid, em, k) in enumerate(slots))


def _month_counts(params: SynthParams) -> dict[MonthKey, int]:
    """Background counts per (source, entity, month): preset totals split by days in span."""
    months: list[date] = []
    days: list[int] = []
    for offset in range((params.end - params.start).days + 1):
        day = params.start + timedelta(days=offset)
        if not months or day.month != months[-1].month:
            months.append(day.replace(day=1))
            days.append(0)
        days[-1] += 1
    p = params.preset
    totals = {("servicenow", "incident"): p.incidents, ("servicenow", "change_request"): p.changes}
    totals |= {("servicenow", "problem"): p.problems, ("jira", "issue"): p.jira_issues}
    totals |= {("monitoring", "event"): p.events}
    counts: dict[MonthKey, int] = {}
    for (source, entity), total in totals.items():
        if source in params.sources:
            split = zip(months, _split(total, days), strict=True)
            counts |= {(source, entity, m): n for m, n in split}
    if "monitoring" in params.sources:
        per_day = p.metric_services * _METRICS_PER_SERVICE
        pairs = zip(months, days, strict=True)
        counts |= {("monitoring", "metric_daily", m): per_day * n for m, n in pairs}
    return counts


def _plan(stub: Catalog, params: SynthParams, planners: Sequence[Planner]) -> Catalog:
    counts = dict(stub.month_counts)
    for planner in planners:
        for key, extra in planner(stub, params).items():
            if key[0] in params.sources:
                counts[key] = counts.get(key, 0) + extra
    if any(n < 0 for n in counts.values()):
        msg = "planned plant counts make a month count negative"
        raise SynthUsageError(msg, key="preset")
    ordered = {key: counts[key] for key in sorted(counts)}
    seq: dict[MonthKey, int] = {}
    running: dict[tuple[str, str], int] = {}
    for (source, entity, month), n in ordered.items():
        seq[source, entity, month] = running.get((source, entity), 1)
        running[source, entity] = seq[source, entity, month] + n
    return dataclasses.replace(stub, month_counts=ordered, seq_start=seq)


def default_planners() -> tuple[Planner, ...]:
    """`planned_counts` of the plant units U11-10..U11-15; they land with T11-10..T11-15."""
    return ()


def build_catalog(
    seed: int, params: SynthParams, *, planners: Sequence[Planner] | None = None
) -> Catalog:
    """Build the catalog; `planners` defaults to `default_planners()` (injection for tests)."""
    klass = params.preset.catalog_class
    rng_c = stream_rng(seed, STREAM_CATALOG + ":" + klass)
    rng_p = stream_rng(seed, STREAM_PLANTS + ":" + klass)
    idx = rng_c.choice(len(pools.ORG_NAMES), size=params.preset.orgs, replace=False)
    orgs = [
        OrgRow(_hex_id(rng_c), pools.ORG_NAMES[i], f"CC-{1000 + 100 * (k + 1)}")
        for k, i in enumerate(idx)
    ]
    teams = _teams(rng_c, params, orgs)
    d = _services(rng_c, params, len(teams))
    cis, rels, appls = _cis(rng_c, d)
    projects = _projects(orgs)
    plants, t1, t5 = _plants(rng_p, d, params, [t.sys_id for t in teams], appls)
    for t in (t1, t5):
        teams[t] = dataclasses.replace(teams[t], mttr_multiplier=1.0, reassign_extra=0.0)
    org_key = {o.sys_id: p.key for o, p in zip(orgs, projects, strict=True)}
    services = []
    for i, sid in enumerate(d.ids):
        owner, support = teams[int(d.owner[i])], teams[int(d.support[i])]
        services.append(ServiceRow(
            sid, d.names[i], int(d.crit[i]), owner.sys_id, support.sys_id, float(d.inc[i]),
            float(d.evt[i]), org_key[owner.org_sys_id], d.names[i], owner.cost_center,
            round(float(d.run_cost[i]), 2), round(float(d.downtime[i]), 2),
        ))  # fmt: skip
    stub = Catalog(
        tuple(orgs), tuple(teams), tuple(services), tuple(cis), tuple(rels), tuple(projects),
        _change_schedule(seed, params), plants, _month_counts(params), {},
    )  # fmt: skip
    return _plan(stub, params, default_planners() if planners is None else planners)


__all__ = ["Catalog", "Planner", "build_catalog", "default_planners"]
