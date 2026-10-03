"""Tests for tools.synth.flatten (U11-18): UT11-22, UT11-23, UT11-118."""

import json
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.connectors import jira as connector_jira
from herness.connectors.jira import JIRA_ISSUE_COLUMNS
from herness.connectors.monitoring.base import EVENT_COLUMNS, METRIC_COLUMNS
from herness.core.errors import SchemaViolation
from herness.store.lake import META_COLUMNS, LakeWriter
from tools.synth.catalog import build_catalog
from tools.synth.flatten import (
    MAX_ROWS,
    SYNTH_CUSTOM_FIELD_IDS,
    source_updated_at,
    to_lake_batch,
)
from tools.synth.jira import gen_issues
from tools.synth.jira_changelog import jira_ts
from tools.synth.params import SynthParams, SynthUsageError, load_params
from tools.synth.pii import build_name_list
from tools.synth.servicenow_common import pair, ts_pair
from tools.synth.shards import Shard
from tools.synth.text import TemplateBank

pytestmark = pytest.mark.unit

Rec = dict[str, Any]

_FETCHED = datetime(2024, 4, 1, 7, 30, tzinfo=UTC)
_UPDATED = datetime(2024, 3, 2, 10, 0, tzinfo=UTC)
_META = [name for name, _, _ in META_COLUMNS]


def _params() -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=date(2024, 3, 31),
        sources=("servicenow", "jira"),
        dirty="none",
        fetch_mode="initial",
        params_file=None,
    )


def _incident(i: int, **extra: Any) -> Rec:
    return {
        "sys_id": pair(f"{i:032x}"),
        "number": pair(f"INC{i:07d}"),
        "priority": pair("2", "2 - High"),
        "business_service": pair("a" * 32, "Checkout"),
        "sys_updated_on": ts_pair(_UPDATED),
        **extra,
    }


def _issue(i: int) -> Rec:
    fields = {
        "issuetype": {"name": "Story"},
        "summary": "Pool connections",
        "updated": jira_ts(_UPDATED),
        "customfield_10016": 5,
        "customfield_10050": None,
        "customfield_10060": "Platform Ops",
    }
    changelog: Rec = {"histories": []}
    issue: Rec = {"id": str(10_000 + i), "key": f"CHK-{i}", "fields": fields}
    return issue | {"changelog": changelog, "remotelinks": []}


_EVENT: Rec = {
    "source_tool": "datadog",
    "event_key": "77",
    "ts": "2024-03-02T10:00:00Z",
    "service": "Checkout",
    "host": "web-1",
    "severity_raw": "minor",
    "title": "latency",
    "status": "resolved",
    "dedup_key": "d1",
    "end_ts": "2024-03-02T10:20:00Z",
    "incident_ref": None,
    "_source_key": "datadog:77",
}
_METRIC: Rec = {
    "source_tool": "prometheus",
    "date": "2024-03-02",
    "service": "Checkout",
    "metric_name": "error_rate",
    "value": 0.0125,
    "unit": "ratio",
    "_source_key": "prometheus|error_rate|Checkout|2024-03-02",
}


def _tomb(key: str, deleted_at: str) -> Rec:
    return {"__tombstone__": True, "key": key, "deleted_at": deleted_at}


def _rows(records: Sequence[Rec]) -> list[tuple[Rec, datetime]]:
    return [(rec, _FETCHED + timedelta(minutes=i)) for i, rec in enumerate(records)]


def _check_meta(batch: pa.RecordBatch) -> None:
    for name, dtype, nullable in META_COLUMNS:
        field = batch.schema.field(name)
        assert field.type == dtype, name
        assert field.nullable == nullable, name
    assert batch.schema.names[:8] == _META


def _write(tmp_path: Path, source: str, entity: str, batch: pa.RecordBatch) -> pa.Table:
    """Write through the real LakeWriter (contract validation) and read the file back."""
    with LakeWriter(source, entity, root=tmp_path) as writer:
        writer.write(batch)
        writer.commit()
    files = sorted(tmp_path.glob(f"{source}/{entity}/dt=*/*.parquet"))
    assert files
    return pa.concat_tables([pq.read_table(f) for f in files])


def test_ut11_22_servicenow_rows_and_tombstone(tmp_path: Path) -> None:
    """UT11-22 ServiceNow: 8 metadata columns with spec 02 types, `<field>`/`<field>_display`
    columns, `_payload` the sorted compact JSON; the tombstone has `_deleted` true, NULL
    `_payload` and NULL fields."""
    rec = _incident(1)
    tomb = _tomb("f" * 32, "2024-03-05 08:00:00")
    batch = to_lake_batch("servicenow", "incident", _rows([rec, tomb]), bare_priority=False)
    _check_meta(batch)
    data = batch.to_pylist()
    live, dead = data
    assert live["_record_id"] == f"servicenow:incident:{1:032x}"
    assert live["_source_key"] == f"{1:032x}"
    assert live["_source_updated_at"] == _UPDATED
    assert live["_fetched_at"] == _FETCHED
    assert live["_deleted"] is False
    assert live["_payload"] == json.dumps(rec, separators=(",", ":"), sort_keys=True)
    assert (live["priority"], live["priority_display"]) == ("2", "2 - High")
    assert live["sys_updated_on"] == "2024-03-02 10:00:00"
    assert "u_business_impact" not in batch.schema.names  # no row carries the drift field
    assert dead["_deleted"] is True
    assert dead["_payload"] is None
    assert dead["_record_id"] == "servicenow:incident:" + "f" * 32
    assert dead["_source_updated_at"] == datetime(2024, 3, 5, 8, tzinfo=UTC)
    assert all(dead[name] is None for name in batch.schema.names[8:])
    table = _write(tmp_path, "servicenow", "incident", batch)
    assert table.num_rows == 2


def test_ut11_22_bare_priority_and_drift_field() -> None:
    """UT11-22 `bare_priority` renders the priority display as the bare value; a drift field
    carried by one row becomes a column, NULL on the rows without it."""
    impact = pair("1", "1 - High")
    rows = _rows([_incident(1, u_business_impact=impact), _incident(2)])
    batch = to_lake_batch("servicenow", "incident", rows, bare_priority=True)
    data = batch.to_pylist()
    assert [r["priority_display"] for r in data] == ["2", "2"]
    assert json.loads(data[0]["_payload"])["priority"] == {"value": "2", "display_value": "2"}
    assert [r["u_business_impact"] for r in data] == ["1", None]


def test_ut11_22_servicenow_columns_follow_fetch_fields() -> None:
    """UT11-22 ServiceNow columns: `sys_id`, `sys_updated_on` first, `sys_class_name` on
    `cmdb_ci`, fields not in the fetch list are not columns."""
    ci = {
        "sys_id": pair("c" * 32),
        "name": pair("checkout-app"),
        "sys_class_name": pair("cmdb_ci_appl", "Application"),
        "sys_updated_on": ts_pair(_UPDATED),
        "not_fetched": pair("x"),
    }
    batch = to_lake_batch("servicenow", "cmdb_ci", _rows([ci]), bare_priority=False)
    names = batch.schema.names[8:]
    head = ["sys_id", "sys_updated_on", "sys_class_name"]
    assert names[:6] == [f"{h}{s}" for h in head for s in ("", "_display")]
    assert "not_fetched" not in names


def test_ut11_22_jira_event_metric_rows(tmp_path: Path) -> None:
    """UT11-22 Jira, event and metric rows: metadata types, keys and update times; monitoring
    columns are the impl 01 names as strings; Jira tombstones are NULL rows."""
    jira = to_lake_batch(
        "jira", "issue", _rows([_issue(1), _tomb("10009", jira_ts(_UPDATED))]), bare_priority=False
    )
    _check_meta(jira)
    issue, tomb = jira.to_pylist()
    assert (issue["_source_key"], issue["_source_updated_at"]) == ("10001", _UPDATED)
    assert tomb["_deleted"] is True
    assert all(tomb[name] is None for name in jira.schema.names[8:])
    _write(tmp_path, "jira", "issue", jira)
    event = to_lake_batch("monitoring", "event", _rows([_EVENT]), bare_priority=False)
    _check_meta(event)
    assert event.schema.names[8:] == list(EVENT_COLUMNS)
    row = event.to_pylist()[0]
    assert row["_source_key"] == "datadog:77"
    assert row["_source_updated_at"] == _UPDATED
    assert row["incident_ref"] is None
    _write(tmp_path, "monitoring", "event", event)
    metric = to_lake_batch("monitoring", "metric_daily", _rows([_METRIC]), bare_priority=False)
    _check_meta(metric)
    assert metric.schema.names[8:] == list(METRIC_COLUMNS)
    assert {metric.schema.field(n).type for n in METRIC_COLUMNS} == {pa.string()}
    row = metric.to_pylist()[0]
    assert row["value"] == "0.0125"
    assert row["_source_updated_at"] == datetime(2024, 3, 3, tzinfo=UTC)
    _write(tmp_path, "monitoring", "metric_daily", metric)


def test_ut11_22_source_updated_at_per_shape() -> None:
    """UT11-22 `_source_updated_at` per record shape (also the fetch `updated_at_of`)."""
    assert source_updated_at("servicenow", "incident", _incident(1)) == _UPDATED
    assert source_updated_at("jira", "issue", _issue(1)) == _UPDATED
    assert source_updated_at("monitoring", "event", _EVENT) == _UPDATED
    assert source_updated_at("monitoring", "metric_daily", _METRIC) == datetime(
        2024, 3, 3, tzinfo=UTC
    )
    tomb = _tomb("1", "2024-03-02 10:00:00")
    assert source_updated_at("servicenow", "incident", tomb) == _UPDATED


def test_ut11_22_usage_errors() -> None:
    """UT11-22 unknown source or entity and more than 131,072 rows are usage errors."""
    rows = _rows([_incident(1)])
    with pytest.raises(SynthUsageError):
        to_lake_batch("files", "incident", rows, bare_priority=False)
    with pytest.raises(SynthUsageError):
        to_lake_batch("servicenow", "sys_user", rows, bare_priority=False)
    with pytest.raises(SynthUsageError):
        to_lake_batch("monitoring", "trace", rows, bare_priority=False)
    with pytest.raises(SynthUsageError):
        to_lake_batch("servicenow", "incident", rows * (MAX_ROWS + 1), bare_priority=False)


@pytest.mark.parametrize(
    ("source", "entity", "record"),
    [
        ("servicenow", "incident", {k: v for k, v in _incident(1).items() if k != "sys_id"}),
        ("servicenow", "incident", _incident(1, sys_id=pair(""))),
        ("jira", "issue", {k: v for k, v in _issue(1).items() if k != "id"}),
        ("monitoring", "event", {k: v for k, v in _EVENT.items() if k != "_source_key"}),
        ("servicenow", "incident", {"__tombstone__": True, "deleted_at": "2024-03-02 10:00:00"}),
    ],
)
def test_ut11_23_missing_key_field(source: str, entity: str, record: Rec) -> None:
    """UT11-23 a row without its key field (`sys_id`, Jira `id`, `_source_key`, tombstone
    `key`) raises `SchemaViolation`."""
    with pytest.raises(SchemaViolation):
        to_lake_batch(source, entity, _rows([record]), bare_priority=False)


def test_ut11_23_missing_watermark() -> None:
    """UT11-23 a ServiceNow row without `sys_updated_on` raises `SchemaViolation`."""
    record = {k: v for k, v in _incident(1).items() if k != "sys_updated_on"}
    with pytest.raises(SchemaViolation):
        to_lake_batch("servicenow", "incident", _rows([record]), bare_priority=False)


@pytest.fixture(scope="module")
def issues() -> list[Rec]:
    params = _params()
    out = gen_issues(
        build_catalog(7, params),
        params,
        Shard("jira", "issue", date(2024, 3, 1), 600, 501, 0),
        np.random.default_rng(118),
        TemplateBank(),
        build_name_list(7),
    )
    return out.records


def test_ut11_118_jira_columns_through_flatten_issue(
    issues: list[Rec], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-118 `flatten_issue` is called once per issue; the columns after the metadata are
    `JIRA_ISSUE_COLUMNS` then the custom field ids, in order; values equal its output."""
    assert any(rec["changelog"]["histories"] for rec in issues)
    assert any(rec["remotelinks"] for rec in issues)
    real = connector_jira.flatten_issue
    calls: list[Mapping[str, object]] = []

    def spy(issue: Mapping[str, object], **kwargs: Any) -> dict[str, str | None]:
        calls.append(issue)
        return real(issue, **kwargs)

    monkeypatch.setattr(connector_jira, "flatten_issue", spy)
    batch = to_lake_batch("jira", "issue", _rows(issues), bare_priority=False)
    assert len(calls) == len(issues)
    assert batch.schema.names[8:] == [*JIRA_ISSUE_COLUMNS, *SYNTH_CUSTOM_FIELD_IDS]
    for rec, row in zip(issues, batch.to_pylist(), strict=True):
        expected = real(
            rec,
            changelog=rec["changelog"]["histories"],
            remotelinks=rec["remotelinks"],
            custom_field_ids=SYNTH_CUSTOM_FIELD_IDS,
        )
        assert {name: row[name] for name in expected} == expected
        assert row["_source_key"] == rec["id"]


def test_ut11_118_drift_shard_without_cost_field(issues: list[Rec]) -> None:
    """UT11-118 a Jira shard whose issues lack the cost field has no cost column."""
    trimmed = [
        {**rec, "fields": {k: v for k, v in rec["fields"].items() if k != "customfield_10050"}}
        for rec in issues[:5]
    ]
    batch = to_lake_batch("jira", "issue", _rows(trimmed), bare_priority=False)
    assert batch.schema.names[8:] == [*JIRA_ISSUE_COLUMNS, "customfield_10016", "customfield_10060"]
