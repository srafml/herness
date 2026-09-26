"""Jira Cloud-shaped issues of the synthetic lake (U11-08, design §5.1.2 and §5.1.3).

Each record is what `GET /rest/api/3/search?expand=changelog` returns for one issue, plus
the `remotelinks` list the connector fetches separately. Plants (T2, T6 epics) are added
by their own units. The status lifecycle and changelog live in
`tools.synth.jira_changelog`, INC/CHG mentions in `tools.synth.jira_links`.
"""

import dataclasses
import math
from collections import defaultdict
from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Any, Final

import numpy as np

from tools.synth import jira_changelog as lifecycle
from tools.synth import jira_links
from tools.synth.catalog_rows import Catalog, ProjectRow, ServiceRow
from tools.synth.jira_changelog import CATEGORIES, STATUS_IDS, jira_ts
from tools.synth.param_groups import JiraParams
from tools.synth.params import SynthParams, SynthUsageError
from tools.synth.pii import inject_pii
from tools.synth.servicenow_common import arrival_times, span_end
from tools.synth.shards import IncidentTimeIndex, Shard
from tools.synth.text import TemplateBank, render_jira_text

Issue = dict[str, Any]

SOURCE: Final = "jira"
ENTITY: Final = "issue"
_UPDATE_LAG_S: Final = 2 * 3600.0  # updated = latest change + U(0, 2 h), as for ServiceNow
_COST_PER_POINT: Final = (2_500.0, 4_000.0)
_COST_ROUND: Final = 1_000
_LINK_RATE: Final = 0.10  # issues with one "Relates" issue link; the spec leaves it open
_LABELS: Final = ("reliability", "tech-debt", "customer", "security", "performance")
_MAX_LABELS: Final = 2
_LEAVES: Final = ("story", "bug", "task")
_PARENT_KINDS: Final[dict[str, tuple[str, ...]]] = {"epic": ("initiative",), "feature": ("epic",)}
_EVEN: Final = 0.5
_RELATES: Final = {"name": "Relates", "inward": "relates to", "outward": "relates to"}


@dataclasses.dataclass(frozen=True, slots=True)
class IssueBatch:
    """Background issues of one shard with their PII rows."""

    records: list[Issue]
    pii: list[dict[str, object]]  # {record_id, field, start, end, type}


@dataclasses.dataclass(slots=True)
class _Draft:
    """One issue before rendering: identity, placement, hierarchy and points."""

    seq: int
    type: str
    service: ServiceRow
    project: ProjectRow
    created: datetime
    parent: int | None = None  # index of the parent draft
    points: int | None = None

    @property
    def key(self) -> str:
        return f"{self.project.key}-{self.seq}"

    @property
    def id(self) -> str:
        return str(self.project.id_base + self.seq)


@dataclasses.dataclass(frozen=True, slots=True)
class _Ctx:
    cat: Catalog
    params: SynthParams
    bank: TemplateBank
    names: Sequence[tuple[str, str]]
    index: IncidentTimeIndex | None
    end: datetime
    no_component_rate: float
    teams: dict[str, str]  # team sys_id -> name


def _check_shard(shard: Shard) -> None:
    if shard.source != SOURCE or shard.entity != ENTITY:
        msg = f"shard must be {SOURCE}/{ENTITY}"
        raise SynthUsageError(msg, key="shard")


def _drafts(
    cat: Catalog, params: SynthParams, shard: Shard, rng: np.random.Generator
) -> list[_Draft]:
    """Step 1: created times from the arrival model, services uniform, the type mix."""
    projects = {p.key: p for p in cat.projects}
    created = arrival_times(params, shard, rng, shard.n_records)
    services = rng.integers(len(cat.services), size=len(created))
    kinds = sorted(params.jira.issue_types)
    p = np.array([params.jira.issue_types[k] for k in kinds])
    types = rng.choice(len(kinds), size=len(created), p=p)
    out = []
    for i, (at, s, t) in enumerate(zip(created, services, types, strict=True)):
        service = cat.services[int(s)]
        project = projects[service.jira_project]
        out.append(_Draft(shard.seq_start + i, kinds[int(t)], service, project, at))
    return out


def _link_parents(drafts: list[_Draft], rng: np.random.Generator) -> None:
    """Step 2, within the shard and project: a feature's parent is an epic; a story's, bug's
    or task's a feature or an epic (even odds, the other kind when one is missing); an
    epic's an initiative when one exists. No candidate leaves `parent` empty."""
    pools: dict[tuple[str, str], list[int]] = defaultdict(list)
    for i, d in enumerate(drafts):
        pools[d.project.key, d.type].append(i)
    for d in drafts:
        kinds: tuple[str, ...] = _PARENT_KINDS.get(d.type, ())
        if d.type in _LEAVES:
            kinds = ("feature", "epic") if rng.random() < _EVEN else ("epic", "feature")
        pool = next((pools[d.project.key, k] for k in kinds if pools[d.project.key, k]), [])
        if pool:
            d.parent = pool[int(rng.integers(len(pool)))]


def _draw_points(jp: JiraParams, rng: np.random.Generator) -> int:
    values = sorted(jp.story_points)
    p = np.array([jp.story_points[v] for v in values])
    return values[int(rng.choice(len(values), p=p))]


def _roll_up(drafts: list[_Draft], jp: JiraParams, rng: np.random.Generator) -> None:
    """Step 3: points on stories; an epic carries the sum of its stories (direct and through
    its features), an initiative the sum of its epics. An epic or initiative without
    pointed descendants draws one value, so every cost estimate has points."""
    sums: dict[int, int] = defaultdict(int)
    for d in drafts:
        if d.type != "story":
            continue
        d.points = _draw_points(jp, rng)
        up = d.parent
        if up is not None and drafts[up].type == "feature":
            up = drafts[up].parent
        if up is not None and drafts[up].type == "epic":
            sums[up] += d.points
    for level in ("epic", "initiative"):
        for i, d in enumerate(drafts):
            if d.type == level:
                d.points = sums.get(i) or _draw_points(jp, rng)
                if d.parent is not None:
                    sums[d.parent] += d.points


def _cost(d: _Draft, rng: np.random.Generator) -> int | None:
    """Step 3: cost estimate on epics and initiatives, points x U(2,500, 4,000) to 1,000."""
    if d.type not in {"epic", "initiative"} or d.points is None:
        return None
    raw = d.points * float(rng.uniform(*_COST_PER_POINT))
    return math.floor(raw / _COST_ROUND + 0.5) * _COST_ROUND


def _issue_links(d: _Draft, drafts: list[_Draft], rng: np.random.Generator) -> list[Issue]:
    """One "Relates" link to another issue of the shard for `_LINK_RATE` of issues."""
    if rng.random() >= _LINK_RATE:
        return []
    other = drafts[int(rng.integers(len(drafts)))]
    if other is d:
        return []
    return [
        {
            "id": f"{d.id}9",
            "type": dict(_RELATES),
            "outwardIssue": {"id": other.id, "key": other.key},
        }
    ]


def _missing_component_rate(params: SynthParams) -> float:
    """Step 5 rate: `dirty_rates.missing_component` at `default`, 0 at `none`, x heavy."""
    rates = params.dirty_rates
    factor = {"none": 0.0, "default": 1.0, "heavy": rates.heavy_multiplier}[params.dirty]
    return min(1.0, rates.missing_component * factor)


def _mention(ctx: _Ctx, rng: np.random.Generator, d: _Draft, text: str) -> tuple[str, list[Issue]]:
    """Step 6: an INC/CHG number in the description (half) or a remote link (half)."""
    if rng.random() >= ctx.params.jira.ticket_mention_rate:
        return text, []
    ticket = jira_links.pick_ticket(ctx.cat, rng, d.service.sys_id, d.created, ctx.index)
    if ticket is None:
        return text, []
    if rng.random() < _EVEN:
        return f"{text}\nSee {ticket} for the operational history.", []
    return text, [jira_links.remote_link(f"{d.id}1", ticket)]


def _pii(
    ctx: _Ctx, rng: np.random.Generator, d: _Draft, text: str
) -> tuple[str, list[dict[str, object]]]:
    """Step 7: PII spans in `pii.jira_share` of descriptions."""
    pp = ctx.params.pii
    if rng.random() >= pp.jira_share:
        return text, []
    n_spans = int(rng.integers(pp.spans_min, pp.spans_max + 1))
    text, spans = inject_pii(text, "description", rng, ctx.names, n_spans=n_spans)
    record_id = f"{SOURCE}:{ENTITY}:{d.id}"
    return text, [
        {"record_id": record_id, "field": s.field, "start": s.start, "end": s.end, "type": s.type}
        for s in spans
    ]


def _issue(
    ctx: _Ctx, rng: np.random.Generator, d: _Draft, drafts: list[_Draft]
) -> tuple[Issue, list[dict[str, object]]]:
    steps = lifecycle.transitions(ctx.params.jira, rng, d.created, ctx.end)
    status, resolved = lifecycle.status(steps)
    latest = max([d.created, *(s[0] for s in steps)])
    updated = latest + timedelta(seconds=float(rng.uniform(0.0, _UPDATE_LAG_S)))
    text = render_jira_text(ctx.bank, rng, issue_type=d.type, component=d.service.name, theme=None)
    description, remotelinks = _mention(ctx, rng, d, f"h3. Context\n{text.description}")
    description, pii = _pii(ctx, rng, d, description)
    missing = rng.random() < ctx.no_component_rate
    picks = rng.integers(len(_LABELS), size=int(rng.integers(0, _MAX_LABELS + 1)))
    category = {"key": CATEGORIES[status]}
    fields: Issue = {
        "issuetype": {"name": d.type.capitalize()},
        "project": {"key": d.project.key, "name": d.project.name},
        "components": [] if missing else [{"name": d.service.jira_component}],
        "labels": sorted({_LABELS[int(i)] for i in picks}),
        "status": {"name": status, "id": STATUS_IDS[status], "statusCategory": category},
        "created": jira_ts(d.created),
        "resolutiondate": None if resolved is None else jira_ts(resolved),
        "customfield_10016": d.points,
        "customfield_10050": _cost(d, rng),
        "customfield_10060": ctx.teams[d.service.owner_team_sys_id],
        "summary": text.short_description,
        "description": description,
        "updated": jira_ts(updated),
        "issuelinks": _issue_links(d, drafts, rng),
    }
    if d.parent is not None:
        fields["parent"] = {"key": drafts[d.parent].key}
    changelog = lifecycle.changelog(d.id, steps)
    record = {"id": d.id, "key": d.key, "fields": fields, "changelog": changelog}
    return record | {"remotelinks": remotelinks}, pii


def gen_issues(  # noqa: PLR0913 - U11-08 signature plus the optional incident index
    cat: Catalog,
    params: SynthParams,
    shard: Shard,
    rng: np.random.Generator,
    bank: TemplateBank,
    names: Sequence[tuple[str, str]],
    *,
    incident_index: IncidentTimeIndex | None = None,
) -> IssueBatch:
    """Exactly `shard.n_records` background issues numbered from `shard.seq_start`.

    `incident_index` (optional; U11-19 builds it) supplies same-service incident numbers
    for the step 6 mentions; without it the catalog's planned number ranges are used.
    """
    _check_shard(shard)
    drafts = _drafts(cat, params, shard, rng)
    _link_parents(drafts, rng)
    _roll_up(drafts, params.jira, rng)
    teams = {t.sys_id: t.name for t in cat.teams}
    rate = _missing_component_rate(params)
    ctx = _Ctx(cat, params, bank, names, incident_index, span_end(params), rate, teams)
    records: list[Issue] = []
    pii: list[dict[str, object]] = []
    for d in drafts:
        record, spans = _issue(ctx, rng, d, drafts)
        records.append(record)
        pii += spans
    return IssueBatch(records, pii)


__all__ = ["IssueBatch", "gen_issues"]
