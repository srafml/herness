"""Tests for tools.synth.monitoring (U11-09): UT11-12."""

import dataclasses
from collections import Counter
from datetime import UTC, date, datetime, timedelta

import numpy as np
import pytest

from tools.synth.catalog import Catalog, build_catalog
from tools.synth.monitoring import gen_events, gen_metric_daily
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.servicenow import gen_incidents
from tools.synth.shards import IncidentTimeIndex, Shard
from tools.synth.text import TemplateBank

pytestmark = pytest.mark.unit

_EVENT_COLUMNS = (
    "source_tool", "event_key", "ts", "service", "host", "severity_raw", "title", "status",
    "dedup_key", "end_ts", "incident_ref",
)  # fmt: skip
_METRIC_COLUMNS = ("source_tool", "date", "service", "metric_name", "value", "unit")
_TOOLS = {"prometheus", "datadog", "splunk"}
_SEVERITIES = {"critical", "major", "minor", "warning", "info"}
_METRICS = {"availability_pct", "error_rate", "p95_latency_ms", "request_count"}
_MONTH = date(2024, 3, 1)
_WINDOW = timedelta(minutes=30)


@pytest.fixture(scope="module")
def params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("servicenow", "monitoring"),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )


@pytest.fixture(scope="module")
def cat(params: SynthParams) -> Catalog:
    return build_catalog(7, params)


@pytest.fixture(scope="module")
def incidents(cat: Catalog, params: SynthParams) -> list[dict[str, dict[str, str]]]:
    shard = Shard("servicenow", "incident", _MONTH, 400, 801, 0)
    rng = np.random.default_rng(21)
    return gen_incidents(cat, params, shard, rng, TemplateBank(), build_name_list(7)).records


def _ts(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _index(incidents: list[dict[str, dict[str, str]]]) -> IncidentTimeIndex:
    """The U11-19 shape built from generated incidents, keyed by business_service sys_id."""
    rows: dict[str, list[tuple[int, str, str]]] = {}
    for r in incidents:
        at = datetime.strptime(r["opened_at"]["value"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
        item = (int(at.timestamp()), r["sys_id"]["value"], r["number"]["value"])
        rows.setdefault(r["business_service"]["value"], []).append(item)
    opened, sys_ids, numbers = {}, {}, {}
    for service, items in rows.items():
        items.sort()
        opened[service] = np.array([t for t, _, _ in items], dtype=np.int64)
        sys_ids[service] = tuple(s for _, s, _ in items)
        numbers[service] = tuple(n for _, _, n in items)
    return IncidentTimeIndex(opened, sys_ids, numbers)


@pytest.fixture(scope="module")
def index(incidents: list[dict[str, dict[str, str]]]) -> IncidentTimeIndex:
    return _index(incidents)


@pytest.fixture(scope="module")
def events(cat: Catalog, params: SynthParams, index: IncidentTimeIndex) -> list[dict[str, object]]:
    shard = Shard("monitoring", "event", _MONTH, 3000, 1, 0)
    return gen_events(cat, params, shard, np.random.default_rng(5), index)


def _s(row: dict[str, object], key: str) -> str:
    value = row[key]
    assert isinstance(value, str)
    return value


def test_ut11_12_incident_ref_only_near_that_incident(
    events: list[dict[str, object]],
    incidents: list[dict[str, dict[str, str]]],
    cat: Catalog,
) -> None:
    """UT11-12 incident_ref is set only on events within +/-30 min of that incident, on
    the incident's service."""
    by_number = {r["number"]["value"]: r for r in incidents}
    names = {s.sys_id: s.name for s in cat.services}
    refs = [e for e in events if e["incident_ref"] is not None]
    assert refs
    for event in refs:
        incident = by_number[_s(event, "incident_ref")]
        opened = datetime.strptime(incident["opened_at"]["value"], "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=UTC
        )
        delta = _ts(_s(event, "ts")) - opened
        assert abs(delta) <= _WINDOW
        assert event["service"] == names[incident["business_service"]["value"]]
        assert event["severity_raw"] != "info"


def test_ut11_12_near_incident_shares(
    events: list[dict[str, object]], index: IncidentTimeIndex, cat: Catalog
) -> None:
    """UT11-12 about half of non-info events on services with incidents lie near one, and
    about 90 % of those carry incident_ref; info events never do."""
    with_incidents = {s.name for s in cat.services if s.sys_id in index.opened_at}
    candidates = [
        e for e in events if e["severity_raw"] != "info" and e["service"] in with_incidents
    ]
    refs = sum(e["incident_ref"] is not None for e in candidates)
    assert 0.35 < refs / len(candidates) < 0.55
    assert all(e["incident_ref"] is None for e in events if e["severity_raw"] == "info")


def test_ut11_12_event_source_key_and_fields(events: list[dict[str, object]]) -> None:
    """UT11-12 events carry the design field set; `_source_key` = `<source_tool>:<event_key>`."""
    assert len(events) == 3000
    keys = set()
    for event in events:
        assert set(event) == {*_EVENT_COLUMNS, "_source_key"}
        assert event["_source_key"] == f"{event['source_tool']}:{event['event_key']}"
        keys.add(event["_source_key"])
        assert event["source_tool"] in _TOOLS
        assert event["severity_raw"] in _SEVERITIES
        ts = _ts(_s(event, "ts"))
        if event["status"] == "resolved":
            assert _ts(_s(event, "end_ts")) > ts
        else:
            assert event["status"] == "firing"
            assert event["end_ts"] is None
        assert _s(event, "host")
        assert _s(event, "title")
        assert _s(event, "dedup_key")
    assert len(keys) == 3000


def test_ut11_12_event_mixes(events: list[dict[str, object]], cat: Catalog) -> None:
    """UT11-12 tools are uniform, severities follow the mix, services follow event weight."""
    tools = Counter(e["source_tool"] for e in events)
    assert all(850 < tools[t] < 1150 for t in _TOOLS)
    sev = Counter(e["severity_raw"] for e in events)
    assert sev["warning"] > sev["minor"] > sev["major"] > sev["critical"]
    assert 300 < sev["info"] < 600
    zero = {s.name for s in cat.services if s.event_weight == 0.0}
    assert not zero & {e["service"] for e in events}
    minutes = [
        (_ts(_s(e, "end_ts")) - _ts(_s(e, "ts"))).total_seconds() / 60
        for e in events
        if e["end_ts"] is not None
    ]
    assert 15 < float(np.median(minutes)) < 26


def test_ut11_12_events_without_incidents_keep_random_time(
    cat: Catalog, params: SynthParams
) -> None:
    """UT11-12 with an empty index no event carries incident_ref."""
    empty = IncidentTimeIndex({}, {}, {})
    shard = Shard("monitoring", "event", _MONTH, 300, 1, 0)
    out = gen_events(cat, params, shard, np.random.default_rng(2), empty)
    assert all(e["incident_ref"] is None for e in out)
    stamps = [_ts(_s(e, "ts")) for e in out]
    assert all(
        datetime(2024, 3, 1, tzinfo=UTC) <= t < datetime(2024, 4, 1, tzinfo=UTC) for t in stamps
    )


@pytest.fixture(scope="module")
def metrics(cat: Catalog, params: SynthParams) -> list[dict[str, object]]:
    shard = Shard("monitoring", "metric_daily", _MONTH, 20 * 4 * 31, 1, 0)
    p1 = frozenset({(cat.services[0].sys_id, date(2024, 3, 5))})
    return gen_metric_daily(cat, params, shard, np.random.default_rng(8), p1, {})


def test_ut11_12_metric_source_key_and_rows(metrics: list[dict[str, object]], cat: Catalog) -> None:
    """UT11-12 one row per service, day and metric; `_source_key` =
    `<source_tool>|<metric_name>|<service>|<date>`."""
    assert len(metrics) == 20 * 4 * 31
    names = [s.name for s in cat.services[:20]]
    for row in metrics:
        assert set(row) == {*_METRIC_COLUMNS, "_source_key"}
        expected = f"{row['source_tool']}|{row['metric_name']}|{row['service']}|{row['date']}"
        assert row["_source_key"] == expected
        assert row["source_tool"] in _TOOLS
        assert row["metric_name"] in _METRICS
        assert row["service"] in names
        date.fromisoformat(_s(row, "date"))
    assert len({r["_source_key"] for r in metrics}) == len(metrics)


def test_ut11_12_metric_values(metrics: list[dict[str, object]], cat: Catalog) -> None:
    """UT11-12 availability in [99.5, 99.99] except the dip on P1 days; other metric ranges."""
    first = cat.services[0].name
    for row in metrics:
        value = row["value"]
        assert isinstance(value, float)
        name = row["metric_name"]
        if name == "availability_pct":
            if row["service"] == first and row["date"] == "2024-03-05":
                assert 99.5 - 3.0 <= value <= 99.99 - 0.5
            else:
                assert 99.5 <= value <= 99.99
        elif name == "error_rate":
            assert 0.001 <= value <= 0.02
        elif name == "p95_latency_ms":
            assert value > 0
        else:
            assert value == int(value) >= 0
    latency = [r["value"] for r in metrics if r["metric_name"] == "p95_latency_ms"]
    assert 200 < float(np.median(latency)) < 310  # type: ignore[arg-type]


def test_ut11_12_request_count_follows_weight_and_day(
    metrics: list[dict[str, object]], cat: Catalog
) -> None:
    """UT11-12 request_count = 10,000 x event weight share x day factor x U(0.95, 1.05)."""
    total = sum(s.event_weight for s in cat.services)
    top = max(cat.services[:20], key=lambda s: s.event_weight)
    counts = {
        _s(r, "date"): r["value"]
        for r in metrics
        if r["metric_name"] == "request_count" and r["service"] == top.name
    }
    base = 10_000 * top.event_weight / total
    weekday, sunday = counts["2024-03-06"], counts["2024-03-10"]  # Wednesday, Sunday
    assert isinstance(weekday, float)
    assert isinstance(sunday, float)
    assert 0.85 * base <= weekday <= 1.2 * base
    assert sunday < 0.5 * weekday


def test_ut11_12_wrong_shards_rejected(
    cat: Catalog, params: SynthParams, index: IncidentTimeIndex
) -> None:
    """UT11-12 shards of another entity raise SynthUsageError."""
    rng = np.random.default_rng(1)
    metric = Shard("monitoring", "metric_daily", _MONTH, 1, 1, 0)
    event = Shard("monitoring", "event", _MONTH, 1, 1, 0)
    with pytest.raises(SynthUsageError):
        gen_events(cat, params, metric, rng, index)
    with pytest.raises(SynthUsageError):
        gen_metric_daily(cat, params, event, rng, frozenset(), {})


def test_ut11_12_catalog_without_event_weight_rejected(
    cat: Catalog, params: SynthParams, index: IncidentTimeIndex
) -> None:
    """UT11-12 a catalog whose services all have zero event weight raises SynthUsageError."""
    silent = tuple(dataclasses.replace(s, event_weight=0.0) for s in cat.services)
    shard = Shard("monitoring", "event", _MONTH, 1, 1, 0)
    with pytest.raises(SynthUsageError):
        gen_events(
            dataclasses.replace(cat, services=silent),
            params,
            shard,
            np.random.default_rng(1),
            index,
        )
