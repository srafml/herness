"""Tests for tools.synth.jira (U11-08): UT11-11."""

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from tools.synth import jira_links
from tools.synth.catalog import Catalog, build_catalog
from tools.synth.jira import IssueBatch, gen_issues
from tools.synth.jira_changelog import CATEGORIES, DONE, TODO
from tools.synth.param_groups import JiraParams, PiiParams
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.shards import IncidentTimeIndex, Shard
from tools.synth.text import TemplateBank

pytestmark = pytest.mark.unit

_END_DT = datetime(2024, 4, 1, tzinfo=UTC)
_POINTS = {1, 2, 3, 5, 8, 13}
_TS = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}\+0000")
_TICKET = re.compile(r"\b(INC|CHG)(\d{7})\b")
_FIELDS = {
    "issuetype", "project", "components", "labels", "status", "created", "resolutiondate",
    "customfield_10016", "customfield_10050", "customfield_10060", "summary", "description",
    "updated", "issuelinks",
}  # fmt: skip
_PARENT_TYPES = {
    "Epic": {"Initiative"},
    "Feature": {"Epic"},
    "Story": {"Feature", "Epic"},
    "Bug": {"Feature", "Epic"},
    "Task": {"Feature", "Epic"},
}


def _params(dirty: str = "none") -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("servicenow", "jira"),
        dirty=dirty,  # type: ignore[arg-type]
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return _params()


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(7, params)


@pytest.fixture(scope="module")
def bank() -> TemplateBank:
    return TemplateBank()


@pytest.fixture(scope="module")
def names() -> tuple[tuple[str, str], ...]:
    return build_name_list(7)


def _shard(n: int = 600, month: date = date(2024, 3, 1), entity: str = "issue") -> Shard:
    return Shard("jira", entity, month, n, 501, 0)


@pytest.fixture(scope="module")
def batch(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> IssueBatch:
    return gen_issues(cat, params, _shard(), np.random.default_rng(3), bank, names)


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%dT%H:%M:%S.%f%z")


def _type(issue: dict[str, Any]) -> str:
    name = issue["fields"]["issuetype"]["name"]
    assert isinstance(name, str)
    return name


def test_ut11_11_identity_and_field_set(batch: IssueBatch, cat: Catalog) -> None:
    """UT11-11 id is project id_base + seq, key is <PROJECT>-<seq>; field set as designed."""
    bases = {p.key: p.id_base for p in cat.projects}
    assert len(batch.records) == 600
    for i, issue in enumerate(batch.records):
        project = issue["fields"]["project"]["key"]
        assert issue["key"] == f"{project}-{501 + i}"
        assert issue["id"] == str(bases[project] + 501 + i)
        assert set(issue) == {"id", "key", "fields", "changelog", "remotelinks"}
        assert set(issue["fields"]) - {"parent"} == _FIELDS
        assert _TS.fullmatch(issue["fields"]["created"])
        assert _TS.fullmatch(issue["fields"]["updated"])
    assert len({i["id"] for i in batch.records}) == 600


def test_ut11_11_type_mix(batch: IssueBatch) -> None:
    """UT11-11 issue types follow the .01/.05/.14/.80 mix (split .60/.25/.15)."""
    types = Counter(_type(i) for i in batch.records)
    assert set(types) <= {"Initiative", "Epic", "Feature", "Story", "Bug", "Task"}
    assert types["Story"] > types["Bug"] > types["Task"] > types["Epic"]
    assert types["Feature"] > types["Epic"]
    assert 0.70 < (types["Story"] + types["Bug"] + types["Task"]) / 600 < 0.90


def test_ut11_11_every_parent_key_exists(batch: IssueBatch) -> None:
    """UT11-11 every parent key exists in the shard, in the same project, of an allowed type."""
    by_key = {i["key"]: i for i in batch.records}
    with_parent = 0
    for issue in batch.records:
        parent = issue["fields"].get("parent")
        if parent is None:
            assert _type(issue) == "Initiative" or _type(issue) in _PARENT_TYPES
            continue
        with_parent += 1
        target = by_key[parent["key"]]
        assert target["fields"]["project"]["key"] == issue["fields"]["project"]["key"]
        assert _type(target) in _PARENT_TYPES[_type(issue)]
    assert with_parent > 400
    assert not any("parent" in i["fields"] for i in batch.records if _type(i) == "Initiative")


def test_ut11_11_points_and_cost(batch: IssueBatch) -> None:
    """UT11-11 story points in {1,2,3,5,8,13}; epics sum their stories; costs on epics only."""
    by_key = {i["key"]: i for i in batch.records}
    sums: Counter[str] = Counter()
    for issue in batch.records:
        points, kind = issue["fields"]["customfield_10016"], _type(issue)
        if kind == "Story":
            assert points in _POINTS
            up = issue["fields"].get("parent", {}).get("key")
            if up is not None and _type(by_key[up]) == "Feature":
                up = by_key[up]["fields"].get("parent", {}).get("key")
            if up is not None:
                sums[up] += points
        elif kind in {"Bug", "Task", "Feature"}:
            assert points is None
        cost = issue["fields"]["customfield_10050"]
        if kind in {"Epic", "Initiative"}:
            assert cost % 1000 == 0
            assert points * 2500 - 500 <= cost <= points * 4000 + 500
        else:
            assert cost is None
    for issue in batch.records:
        if _type(issue) == "Epic":
            expected = sums.get(issue["key"])
            points = issue["fields"]["customfield_10016"]
            assert points == expected if expected else points in _POINTS


def test_ut11_11_changelog_consistent_with_resolutiondate(batch: IssueBatch) -> None:
    """UT11-11 changelog statuses agree with status, category and resolutiondate."""
    statuses: Counter[str] = Counter()
    reentered = 0
    for issue in batch.records:
        fields = issue["fields"]
        histories = issue["changelog"]["histories"]
        assert issue["changelog"]["total"] == len(histories)
        stamps = [_ts(h["created"]) for h in histories]
        assert stamps == sorted(stamps)
        assert all(_ts(fields["created"]) <= s <= _END_DT for s in stamps)
        current = TODO
        for history in histories:
            (item,) = history["items"]
            assert item["field"] == "status"
            assert item["fromString"] == current
            current = item["toString"]
        status = fields["status"]["name"]
        statuses[status] += 1
        assert status == current
        assert fields["status"]["statusCategory"]["key"] == CATEGORIES[status]
        if status == DONE:
            assert fields["resolutiondate"] == histories[-1]["created"]
        else:
            assert fields["resolutiondate"] is None
        reentered += sum(h["items"][0]["toString"] == TODO for h in histories)
        latest = max([_ts(fields["created"]), *stamps])
        assert latest <= _ts(fields["updated"]) <= latest + timedelta(hours=2)
    assert statuses[DONE] > statuses["In Progress"] > 0
    assert 0.05 < reentered / 600 < 0.20


def test_ut11_11_carry_over_resolves_after_creation_month(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 with carry-over at 1.0 every resolved issue finishes after its creation month."""
    jira = JiraParams(carry_over_rate=1.0)
    p = params.model_copy(update={"jira": jira})
    out = gen_issues(cat, p, _shard(200, date(2024, 1, 1)), np.random.default_rng(5), bank, names)
    resolved = [i["fields"]["resolutiondate"] for i in out.records]
    assert all(r is None or _ts(r).month == 2 for r in resolved)
    assert sum(r is not None for r in resolved) > 150


def _tickets(issue: dict[str, Any]) -> list[tuple[str, int]]:
    links = [r["object"]["title"] for r in issue["remotelinks"]]
    texts = [issue["fields"]["description"], *links]
    return [(m.group(1), int(m.group(2))) for t in texts for m in _TICKET.finditer(t)]


def _index(cat: Catalog) -> IncidentTimeIndex:
    """Every service: one incident each 12 h from 40 days before March; numbers encode it."""
    base = int(datetime(2024, 3, 1, tzinfo=UTC).timestamp()) - 40 * 86400
    times = np.arange(base, base + 71 * 86400, 43200, dtype=np.int64)
    opened, numbers = {}, {}
    for k, s in enumerate(cat.services):
        opened[s.sys_id] = times
        numbers[s.sys_id] = tuple(f"INC{8_000_000 + k * 1000 + j:07d}" for j in range(len(times)))
    return IncidentTimeIndex(opened, numbers, numbers)


def _mention_params(params: SynthParams) -> SynthParams:
    update = {"jira": JiraParams(ticket_mention_rate=1.0), "pii": PiiParams(jira_share=0.0)}
    return params.model_copy(update=update)


def test_ut11_11_mentions_from_same_service_index(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 INC mentions come from the same service's incidents of the 30 days before
    `created`; half of the mentions sit in the description, half in remotelinks."""
    p = _mention_params(params)
    position = {s.jira_component: k for k, s in enumerate(cat.services)}
    base = int(datetime(2024, 3, 1, tzinfo=UTC).timestamp()) - 40 * 86400
    rng = np.random.default_rng(9)
    out = gen_issues(cat, p, _shard(300), rng, bank, names, incident_index=_index(cat))
    where: Counter[str] = Counter()
    for issue in out.records:
        found = _tickets(issue)
        assert len(found) <= 1
        if not found:
            continue
        where["remote" if issue["remotelinks"] else "text"] += 1
        kind, seq = found[0]
        if kind == "CHG":
            assert 1 <= seq <= 250
            continue
        k, j = divmod(seq - 8_000_000, 1000)
        assert k == position[issue["fields"]["components"][0]["name"]]
        created = _ts(issue["fields"]["created"]).timestamp()
        assert created - 30 * 86400 <= base + j * 43200 < created
    for link in (r for i in out.records for r in i["remotelinks"]):
        assert link["object"]["url"].startswith("https://servicenow.example.com/")
        assert isinstance(link["id"], int)
    assert where["remote"] > 100
    assert where["text"] > 100


def test_ut11_11_mentions_without_index_use_planned_ranges(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 without an index INC/CHG numbers come from the catalog's planned ranges."""
    out = gen_issues(
        cat, _mention_params(params), _shard(300), np.random.default_rng(9), bank, names
    )
    found = [t for i in out.records for t in _tickets(i)]
    assert {k for k, _ in found} == {"INC", "CHG"}
    assert len(found) > 250
    assert all(1 <= s <= 1200 for k, s in found if k == "INC")
    assert all(1 <= s <= 250 for k, s in found if k == "CHG")


def test_ut11_11_mention_rate_default(batch: IssueBatch) -> None:
    """UT11-11 about 3 % of issues mention a ticket at the default rate."""
    mentioned = sum(bool(_tickets(i)) for i in batch.records)
    assert 3 <= mentioned <= 45


def test_ut11_11_missing_component_follows_dirty_level(
    cat: Catalog, bank: TemplateBank, names: tuple[tuple[str, str], ...], batch: IssueBatch
) -> None:
    """UT11-11 25 % of issues have no component at dirty `default`, none at `none`."""
    assert all(len(i["fields"]["components"]) == 1 for i in batch.records)
    services = {s.jira_component for s in cat.services}
    assert {i["fields"]["components"][0]["name"] for i in batch.records} <= services
    out = gen_issues(cat, _params("default"), _shard(), np.random.default_rng(3), bank, names)
    empty = sum(not i["fields"]["components"] for i in out.records)
    assert 0.18 < empty / 600 < 0.32


def test_ut11_11_pii_spans_in_descriptions(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 PII rows point into the description of the named issue record."""
    p = params.model_copy(update={"pii": PiiParams(jira_share=1.0)})
    out = gen_issues(cat, p, _shard(50), np.random.default_rng(4), bank, names)
    by_id = {f"jira:issue:{i['id']}": i for i in out.records}
    assert len({row["record_id"] for row in out.pii}) == 50
    for row in out.pii:
        assert row["field"] == "description"
        text = by_id[str(row["record_id"])]["fields"]["description"]
        start, end = row["start"], row["end"]
        assert isinstance(start, int)
        assert isinstance(end, int)
        assert 0 <= start < end <= len(text)


def test_ut11_11_default_pii_share_and_determinism(
    cat: Catalog,
    params: SynthParams,
    bank: TemplateBank,
    names: tuple[tuple[str, str], ...],
    batch: IssueBatch,
) -> None:
    """UT11-11 PII on about 1 % of descriptions; equal rng state gives equal issues."""
    assert len({row["record_id"] for row in batch.pii}) <= 20
    again = gen_issues(cat, params, _shard(), np.random.default_rng(3), bank, names)
    assert again.records == batch.records
    assert again.pii == batch.pii


def test_ut11_11_wrong_shard_rejected(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 a shard of another source or entity raises SynthUsageError."""
    rng = np.random.default_rng(1)
    with pytest.raises(SynthUsageError):
        gen_issues(cat, params, _shard(entity="sprint"), rng, bank, names)
    other = Shard("servicenow", "issue", date(2024, 3, 1), 1, 1, 0)
    with pytest.raises(SynthUsageError):
        gen_issues(cat, params, other, rng, bank, names)


def test_ut11_11_empty_shard(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 an empty shard gives an empty batch."""
    out = gen_issues(cat, params, _shard(0), np.random.default_rng(1), bank, names)
    assert out.records == []
    assert out.pii == []


def test_ut11_11_pick_ticket_edges(cat: Catalog) -> None:
    """UT11-11 no mention when the index lacks the service or the window is empty, or when
    the planned range is empty or outside the planned months."""
    rng = np.random.default_rng(1)
    march = datetime(2024, 3, 10, tzinfo=UTC)
    sid = cat.services[0].sys_id
    empty = IncidentTimeIndex({}, {}, {})
    late = IncidentTimeIndex({sid: np.array([int(march.timestamp()) + 60])}, {}, {sid: ("INC1",)})
    for _ in range(20):  # INC or CHG with even odds; INC paths must give None
        for index in (empty, late):
            ticket = jira_links.pick_ticket(cat, rng, sid, march, index)
            assert ticket is None or ticket.startswith("CHG")
    before = datetime(2023, 6, 1, tzinfo=UTC)
    assert jira_links.pick_ticket(cat, rng, sid, before, None) is None
    start = datetime(2024, 1, 1, tzinfo=UTC)  # nothing planned before the first instant
    assert jira_links.pick_ticket(cat, rng, sid, start, None) is None


def test_ut11_11_plan_without_servicenow(
    bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-11 without ServiceNow in the sources no ticket is mentioned."""
    p = load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("jira",),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )
    p = _mention_params(p)
    out = gen_issues(build_catalog(7, p), p, _shard(50), np.random.default_rng(2), bank, names)
    assert not any(_tickets(i) for i in out.records)
