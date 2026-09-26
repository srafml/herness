"""Tests for tools.synth.servicenow (U11-07): UT11-09 and UT11-10."""

import re
from collections import Counter
from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from tools.synth.catalog import Catalog, build_catalog
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.servicenow import (
    IncidentBatch,
    close_code_probs,
    gen_changes,
    gen_cis,
    gen_groups,
    gen_incidents,
    gen_problems,
    gen_rels,
)
from tools.synth.servicenow_incidents import priority_probs
from tools.synth.shards import Shard
from tools.synth.text import ROOT_CAUSE_OPTIONS, TemplateBank

pytestmark = pytest.mark.unit

_END = date(2024, 3, 31)
_END_DT = datetime(2024, 4, 1, tzinfo=UTC)
_TS = re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_HEX32 = re.compile(r"[0-9a-f]{32}")
_INCIDENT_FIELDS = {
    "sys_id", "number", "opened_at", "u_acknowledged_at", "resolved_at", "closed_at",
    "priority", "state", "business_service", "cmdb_ci", "assignment_group",
    "reassignment_count", "reopen_count", "short_description", "description", "close_notes",
    "close_code", "problem_id", "caused_by", "made_sla", "business_duration",
    "u_customer_impact_minutes", "sys_updated_on",
}  # fmt: skip
_CHANGE_FIELDS = {
    "sys_id", "number", "type", "state", "risk", "opened_at", "start_date", "end_date",
    "work_start", "work_end", "business_service", "cmdb_ci", "assignment_group", "close_code",
    "short_description", "description", "sys_updated_on",
}  # fmt: skip
_PROBLEM_FIELDS = {
    "sys_id", "number", "opened_at", "resolved_at", "state", "business_service",
    "assignment_group", "known_error", "cause_notes", "sys_updated_on",
}  # fmt: skip
_GROUP_FIELDS = {
    "sys_id", "name", "parent", "manager", "cost_center", "active", "type", "sys_updated_on",
}  # fmt: skip
_CI_FIELDS = {
    "sys_id", "name", "sys_class_name", "owned_by", "support_group", "cost_center", "company",
}  # fmt: skip
_REL_FIELDS = {"sys_id", "parent", "child", "type"}
_LIMIT_HOURS = {1: 4, 2: 12, 3: 72, 4: 168, 5: 720}
_QUESTIONS = {"root_cause", "change_caused", "repeat_issue", "business_impact", "owning_team"}
_PII_TYPES = {"PERSON", "EMAIL", "PHONE", "IP", "EMPLOYEE_ID", "CARD", "CREDENTIAL", "URL_TOKEN"}
_CLOSE_CODES = {"successful", "successful_with_issues", "unsuccessful", "backed_out"}
_PRIORITY_DISPLAY = {"1 - Critical", "2 - High", "3 - Moderate", "4 - Low", "5 - Planning"}


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=_END,
        sources=("servicenow",),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(7, params)


@pytest.fixture(scope="module")
def bank() -> TemplateBank:
    return TemplateBank()


@pytest.fixture(scope="module")
def names() -> tuple[tuple[str, str], ...]:
    return build_name_list(7)


def _shard(entity: str, n: int, seq_start: int = 1001, month: date = date(2024, 3, 1)) -> Shard:
    return Shard("servicenow", entity, month, n, seq_start, 0)


@pytest.fixture(scope="module")
def batch(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> IncidentBatch:
    rng = np.random.default_rng(11)
    return gen_incidents(cat, params, _shard("incident", 2000), rng, bank, names)


def _v(record: dict[str, Any], field: str) -> str:
    value = record[field]["value"]
    assert isinstance(value, str)
    return value


def _ts(text: str) -> datetime:
    return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)


def _rid(record: dict[str, Any]) -> str:
    return f"servicenow:incident:{_v(record, 'sys_id')}"


def _pairs_ok(record: dict[str, Any]) -> bool:
    return all(
        isinstance(v, dict) and set(v) == {"value", "display_value"} for v in record.values()
    )


def test_ut11_09_incident_field_set_and_numbers(batch: IncidentBatch) -> None:
    """UT11-09 incident field set equals design §5.1.2; numbers INC + 7 digits from seq_start."""
    assert len(batch.records) == 2000
    for i, record in enumerate(batch.records):
        assert set(record) == _INCIDENT_FIELDS
        assert _pairs_ok(record)
        assert _v(record, "number") == f"INC{1001 + i:07d}"
        assert _HEX32.fullmatch(_v(record, "sys_id"))
    assert len({_v(r, "sys_id") for r in batch.records}) == 2000


def test_ut11_09_made_sla_exactly_when_duration_exceeds_limit(batch: IncidentBatch) -> None:
    """UT11-09 made_sla is false exactly when the duration exceeds the priority limit."""
    breached = 0
    for record in batch.records:
        opened = _ts(_v(record, "opened_at"))
        resolved = _v(record, "resolved_at")
        stop = _ts(resolved) if resolved else _END_DT
        limit = timedelta(hours=_LIMIT_HOURS[int(_v(record, "priority"))])
        expected = "false" if stop - opened > limit else "true"
        assert _v(record, "made_sla") == expected
        breached += expected == "false"
    assert 0 < breached < len(batch.records)


def test_ut11_09_timestamps_states_and_open_records(batch: IncidentBatch) -> None:
    """UT11-09 timestamps use the internal format; open records have no resolution fields."""
    states = Counter(record["state"]["display_value"] for record in batch.records)
    assert states["In Progress"] > 0
    assert states["Closed"] > 0
    for record in batch.records:
        fields = ("opened_at", "u_acknowledged_at", "resolved_at", "closed_at")
        stamps = [_v(record, f) for f in fields]
        present = [_ts(s) for s in stamps if s]
        assert all(_TS.fullmatch(s) for s in stamps if s)
        updated = _ts(_v(record, "sys_updated_on"))
        assert max(present) <= updated <= max(present) + timedelta(hours=2)
        resolved = _v(record, "resolved_at")
        if record["state"]["display_value"] == "In Progress":
            assert not resolved
            assert not _v(record, "closed_at")
            assert not _v(record, "business_duration")
        else:
            assert _ts(_v(record, "opened_at")) <= _ts(resolved) <= _END_DT


def test_ut11_09_arrival_and_priority_shape(batch: IncidentBatch) -> None:
    """UT11-09 arrival stays in the shard month, peaks in the day; priorities follow the mix."""
    opened = [_ts(_v(r, "opened_at")) for r in batch.records]
    assert opened == sorted(opened)
    low, high = datetime(2024, 3, 1, tzinfo=UTC), datetime(2024, 4, 1, tzinfo=UTC)
    assert all(low <= t < high for t in opened)
    hours = Counter(t.hour for t in opened)
    assert sum(hours[h] for h in range(10, 16)) > 3 * sum(hours[h] for h in range(6))
    pri = Counter(_v(r, "priority") for r in batch.records)
    assert pri["4"] > pri["3"] > pri["5"]
    assert {r["priority"]["display_value"] for r in batch.records} <= _PRIORITY_DISPLAY
    acked = sum(bool(_v(r, "u_acknowledged_at")) for r in batch.records)
    assert 0.78 < acked / len(batch.records) < 0.92


def test_ut11_09_assignment_group_is_support_team(batch: IncidentBatch, cat: Catalog) -> None:
    """UT11-09 assignment_group is the service support team; the CI belongs to the service."""
    services = {s.sys_id: s for s in cat.services}
    ci_service = {c.sys_id: c.service_sys_id for c in cat.cis}
    for record in batch.records:
        service = services[_v(record, "business_service")]
        assert _v(record, "assignment_group") == service.support_team_sys_id
        assert ci_service[_v(record, "cmdb_ci")] == service.sys_id
        assert record["business_service"]["display_value"] == service.name


def test_ut11_09_labels_and_pii(batch: IncidentBatch, cat: Catalog) -> None:
    """UT11-09 five truth labels per incident; PII on about 3 % of descriptions."""
    assert len(batch.labels) == 5 * len(batch.records)
    by_record: dict[str, dict[str, str]] = {}
    for row in batch.labels:
        by_record.setdefault(row["record_id"], {})[row["question"]] = row["answer"]
    teams = {t.sys_id for t in cat.teams}
    for record in batch.records:
        labels = by_record[_rid(record)]
        assert set(labels) == _QUESTIONS
        assert labels["root_cause"] in ROOT_CAUSE_OPTIONS
        assert labels["change_caused"] == "false"
        assert labels["repeat_issue"] in {"true", "false"}
        assert labels["business_impact"] in {"0", "1", "2", "3"}
        assert labels["owning_team"] == _v(record, "assignment_group")
        assert labels["owning_team"] in teams
    pii_records = {row["record_id"] for row in batch.pii}
    assert 0.01 < len(pii_records) / len(batch.records) < 0.06
    described = {_rid(r): _v(r, "description") for r in batch.records}
    for row in batch.pii:
        assert row["field"] == "description"
        assert 0 <= row["start"] < row["end"] <= len(described[row["record_id"]])
        assert row["type"] in _PII_TYPES


def test_ut11_09_customer_impact_on_p1_p2_only(batch: IncidentBatch) -> None:
    """UT11-09 customer impact only on P1/P2, within U(0.3, 1.0) of the duration."""
    seen = 0
    for record in batch.records:
        impact = _v(record, "u_customer_impact_minutes")
        if not impact:
            continue
        seen += 1
        assert _v(record, "priority") in {"1", "2"}
        opened, resolved = _ts(_v(record, "opened_at")), _v(record, "resolved_at")
        stop = _ts(resolved) if resolved else _END_DT
        minutes = (stop - opened).total_seconds() / 60
        assert 0.3 * minutes - 1 <= int(impact) <= minutes + 1
    assert seen > 0


def test_ut11_09_repeat_cluster_members(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-09 every 80th incident per service is repeat-flavored and reuses the cluster cause."""
    rng = np.random.default_rng(3)
    batch = gen_incidents(cat, params, _shard("incident", 4000), rng, bank, names)
    labels = {(row["record_id"], row["question"]): row["answer"] for row in batch.labels}
    counts: Counter[str] = Counter()
    expected: set[str] = set()
    causes: dict[str, set[str]] = {}
    for record in batch.records:
        service = _v(record, "business_service")
        counts[service] += 1
        if counts[service] % params.problem.incidents_per_problem == 0:
            expected.add(_rid(record))
            causes.setdefault(service, set()).add(labels[_rid(record), "root_cause"])
    repeats = {rid for (rid, q), a in labels.items() if q == "repeat_issue" and a == "true"}
    assert expected
    assert repeats == expected
    assert all(len(c) == 1 for c in causes.values())


def test_ut11_09_deterministic_and_shard_mismatch(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-09 equal rng gives equal output; a shard for another entity is rejected."""
    shard = _shard("incident", 50)
    one = gen_incidents(cat, params, shard, np.random.default_rng(5), bank, names)
    two = gen_incidents(cat, params, shard, np.random.default_rng(5), bank, names)
    assert one == two
    rng = np.random.default_rng(5)
    with pytest.raises(SynthUsageError):
        gen_incidents(cat, params, _shard("problem", 5), rng, bank, names)
    with pytest.raises(SynthUsageError):
        gen_changes(cat, params, _shard("incident", 5), rng, bank)
    with pytest.raises(SynthUsageError):
        gen_problems(cat, params, Shard("jira", "problem", date(2024, 3, 1), 5, 1, 0), rng, bank)


def test_ut11_09_changes(cat: Catalog, params: SynthParams, bank: TemplateBank) -> None:
    """UT11-09 change field set, CHG numbers, planned and work windows, close codes."""
    rng = np.random.default_rng(2)
    rows = gen_changes(cat, params, _shard("change_request", 1500, 7), rng, bank)
    assert len(rows) == 1500
    types = Counter(_v(r, "type") for r in rows)
    assert set(types) <= {"standard", "normal", "emergency"}
    assert types["standard"] > types["normal"] > types["emergency"] > 0
    for i, record in enumerate(rows):
        assert set(record) == _CHANGE_FIELDS
        assert _pairs_ok(record)
        assert _v(record, "number") == f"CHG{7 + i:07d}"
        start, end = _ts(_v(record, "start_date")), _ts(_v(record, "end_date"))
        assert timedelta(hours=1) <= end - start <= timedelta(hours=8)
        assert _ts(_v(record, "opened_at")) <= start
        if _v(record, "work_end"):
            assert abs(_ts(_v(record, "work_start")) - start) <= timedelta(minutes=30)
            assert abs(_ts(_v(record, "work_end")) - end) <= timedelta(minutes=30)
            assert _v(record, "close_code") in _CLOSE_CODES
            assert record["state"]["display_value"] == "Closed"
        else:
            assert not _v(record, "close_code")
    codes = Counter(_v(r, "close_code") for r in rows if _v(r, "close_code"))
    assert codes["successful"] / sum(codes.values()) > 0.8


def test_ut11_09_problems(cat: Catalog, params: SynthParams, bank: TemplateBank) -> None:
    """UT11-09 problem field set, PRB numbers, known_error share, cause notes per service."""
    rows = gen_problems(cat, params, _shard("problem", 400, 1), np.random.default_rng(4), bank)
    assert len(rows) == 400
    notes: dict[str, set[str]] = {}
    for i, record in enumerate(rows):
        assert set(record) == _PROBLEM_FIELDS
        assert _pairs_ok(record)
        assert _v(record, "number") == f"PRB{1 + i:07d}"
        assert _v(record, "known_error") in {"true", "false"}
        notes.setdefault(_v(record, "business_service"), set()).add(_v(record, "cause_notes"))
    assert all(len(n) == 1 for n in notes.values())
    known = sum(_v(r, "known_error") == "true" for r in rows) / len(rows)
    assert 0.3 < known < 0.5


def test_ut11_09_groups_carry_cost_center(cat: Catalog, params: SynthParams) -> None:
    """UT11-09 one group per org and team; each team group carries its org's cost_center."""
    rows = gen_groups(cat, params)
    assert len(rows) == len(cat.orgs) + len(cat.teams)
    by_id = {_v(r, "sys_id"): r for r in rows}
    orgs = {o.sys_id: o for o in cat.orgs}
    for team in cat.teams:
        record = by_id[team.sys_id]
        assert set(record) == _GROUP_FIELDS
        assert _pairs_ok(record)
        assert _v(record, "parent") == team.org_sys_id
        assert _v(record, "cost_center") == orgs[team.org_sys_id].cost_center
        assert _v(record, "sys_updated_on") == f"{params.start.isoformat()} 00:00:00"
        assert record["manager"]["display_value"] == team.manager_name
    for org in cat.orgs:
        assert _v(by_id[org.sys_id], "parent") == ""
        assert _v(by_id[org.sys_id], "cost_center") == org.cost_center


def test_ut11_09_relations(cat: Catalog, params: SynthParams) -> None:
    """UT11-09 one cmdb_rel_ci row per catalog relation with its type display."""
    rows = gen_rels(cat, params)
    assert len(rows) == len(cat.rels)
    for record, rel in zip(rows, cat.rels, strict=True):
        assert set(record) == _REL_FIELDS
        assert _pairs_ok(record)
        assert (_v(record, "parent"), _v(record, "child")) == (rel.parent, rel.child)
        assert record["type"]["display_value"] == rel.type
        assert _HEX32.fullmatch(_v(record, "type"))


def test_ut11_10_cmdb_ci_rows_omit_criticality(cat: Catalog, params: SynthParams) -> None:
    """UT11-10 cmdb_ci rows have no busines_criticality; cmdb_ci_service rows do."""
    ci_rows, service_rows = gen_cis(cat, params)
    assert len(ci_rows) == len(cat.cis)
    assert len(service_rows) == len(cat.services)
    classes = {"cmdb_ci_service", "cmdb_ci_appl", "cmdb_ci_server", "cmdb_ci_db_instance"}
    for record in ci_rows:
        assert "busines_criticality" not in record
        assert set(record) == _CI_FIELDS
        assert _pairs_ok(record)
        assert _v(record, "sys_class_name") in classes
    crit = {s.sys_id: s.criticality for s in cat.services}
    for record in service_rows:
        assert set(record) == _CI_FIELDS | {"busines_criticality"}
        assert _v(record, "sys_class_name") == "cmdb_ci_service"
        assert _v(record, "busines_criticality").startswith(f"{crit[_v(record, 'sys_id')]} - ")


def test_ut11_10_ci_ownership_follows_service(cat: Catalog, params: SynthParams) -> None:
    """UT11-10 every CI carries its service's owner, support group, cost center and company."""
    ci_rows, _ = gen_cis(cat, params)
    services = {s.sys_id: s for s in cat.services}
    teams = {t.sys_id: t for t in cat.teams}
    for record, ci in zip(ci_rows, cat.cis, strict=True):
        service = services[ci.service_sys_id]
        assert _v(record, "owned_by") == service.owner_team_sys_id
        assert _v(record, "support_group") == service.support_team_sys_id
        assert _v(record, "cost_center") == service.cost_center
        assert _v(record, "company") == teams[service.owner_team_sys_id].org_sys_id


def test_ut11_10_service_rows_share_no_pair_with_ci_rows(cat: Catalog, params: SynthParams) -> None:
    """UT11-10 the cmdb_ci_service rows are copies: mutating one leaves cmdb_ci untouched."""
    ci_rows, service_rows = gen_cis(cat, params)
    service_rows[0]["name"]["value"] = "changed"
    assert all(_v(r, "name") != "changed" for r in ci_rows)


def test_ut11_09_business_timezone_empty_and_outside_shards(
    cat: Catalog, params: SynthParams, bank: TemplateBank, names: tuple[tuple[str, str], ...]
) -> None:
    """UT11-09 the hour curve applies in business_timezone; empty and out-of-span shards."""
    ny = params.model_copy(update={"business_timezone": "America/New_York"})
    rng = np.random.default_rng(8)
    batch = gen_incidents(cat, ny, _shard("incident", 3000), rng, bank, names)
    hours = Counter(_ts(_v(r, "opened_at")).hour for r in batch.records)
    peak_utc = sum(hours[h] for h in range(14, 20))  # 10:00-15:59 local lies in 14..20 UTC
    assert peak_utc > 3 * sum(hours[h] for h in range(4, 10))
    empty = gen_incidents(cat, params, _shard("incident", 0), rng, bank, names)
    assert empty == IncidentBatch([], [], [])
    with pytest.raises(SynthUsageError):
        gen_incidents(cat, params, _shard("incident", 3, month=date(2025, 1, 1)), rng, bank, names)
    first = gen_incidents(cat, params, _shard("incident", 300, month=date(2024, 1, 1)), rng,
                          bank, names)  # fmt: skip
    start = f"{params.start.isoformat()} 00:00:00"
    assert min(_v(r, "opened_at") for r in first.records) >= start


def test_ut11_09_changes_in_progress_at_span_end(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-09 changes still running at the span end are not closed and carry no close code."""
    rng = np.random.default_rng(9)
    rows = gen_changes(cat, params, _shard("change_request", 3000), rng, bank)
    states = Counter(r["state"]["display_value"] for r in rows)
    assert states["Implement"] > 0
    for record in rows:
        if record["state"]["display_value"] != "Closed":
            assert not _v(record, "close_code")
            assert not _v(record, "work_end")
        assert _ts(_v(record, "sys_updated_on")) <= _END_DT + timedelta(hours=2)


@pytest.mark.parametrize("zone", ["America/New_York", "Asia/Tokyo"])
@pytest.mark.parametrize("month", [date(2024, 1, 1), date(2024, 3, 1)])
def test_ut11_09_non_utc_zone_keeps_incidents_inside_span(
    cat: Catalog,
    params: SynthParams,
    bank: TemplateBank,
    names: tuple[tuple[str, str], ...],
    zone: str,
    month: date,
) -> None:
    """UT11-09 a non-UTC business_timezone never opens an incident outside the span; no
    negative duration or customer impact (review M1)."""
    shifted = params.model_copy(update={"business_timezone": zone})
    rng = np.random.default_rng(21)
    batch = gen_incidents(cat, shifted, _shard("incident", 20_000, month=month), rng, bank, names)
    lo = max(datetime.combine(params.start, datetime.min.time(), UTC),
             datetime(month.year, month.month, 1, tzinfo=UTC))  # fmt: skip
    hi = min(_END_DT, datetime(month.year, month.month + 1, 1, tzinfo=UTC))
    for record in batch.records:
        opened = _ts(_v(record, "opened_at"))
        assert lo <= opened < hi
        if impact := _v(record, "u_customer_impact_minutes"):
            assert int(impact) >= 0
        if resolved := _v(record, "resolved_at"):
            assert _ts(resolved) >= opened
    assert any(_v(r, "u_customer_impact_minutes") for r in batch.records)


def test_ut11_09_change_opened_at_not_before_span_start(
    cat: Catalog, params: SynthParams, bank: TemplateBank
) -> None:
    """UT11-09 the change lead time never takes opened_at before the span start (review M4)."""
    rng = np.random.default_rng(22)
    rows = gen_changes(cat, params, _shard("change_request", 3000, month=date(2024, 1, 1)), rng,
                       bank)  # fmt: skip
    start = f"{params.start.isoformat()} 00:00:00"
    assert min(_v(r, "opened_at") for r in rows) == start  # early changes were clamped
    assert all(_v(r, "opened_at") <= _v(r, "start_date") for r in rows)


def test_ut11_09_emergency_close_code_boost(params: SynthParams) -> None:
    """UT11-09 emergency changes multiply each non-success close-code share by 3 (review M3)."""
    assert params.change.emergency_failure_multiplier == 3.0
    normal = close_code_probs(params, "normal")
    emergency = close_code_probs(params, "emergency")
    assert set(normal) == set(emergency) == _CLOSE_CODES
    assert sum(emergency.values()) == pytest.approx(1.0)
    for code in _CLOSE_CODES - {"successful"}:
        ratio = emergency[code] / emergency["successful"]
        assert ratio == pytest.approx(3.0 * normal[code] / normal["successful"])
    assert close_code_probs(params, "standard") == pytest.approx(normal)


def test_ut11_09_criticality1_doubles_p1_p2(params: SynthParams) -> None:
    """UT11-09 criticality-1 services double the P1/P2 shares, taken from P4 (review M3)."""
    assert params.priority.criticality1_high_multiplier == 2.0
    base = priority_probs(params, 2)
    crit1 = priority_probs(params, 1)
    assert crit1.sum() == pytest.approx(1.0)
    assert crit1[0] == pytest.approx(2.0 * base[0])
    assert crit1[1] == pytest.approx(2.0 * base[1])
    assert crit1[2] == pytest.approx(base[2])
    assert crit1[3] == pytest.approx(base[3] - base[0] - base[1])
    assert crit1[4] == pytest.approx(base[4])
