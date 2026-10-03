"""Integration flow of the Jira connector into staging (impl 01 IT01-11; §4.5, R-59; T01-18).

Real pieces: the migrated ops store bound as the resilience backend (breaker, retry), the
connector with its ``SourceHttp`` over the replayed ``cloud_sync_it`` cassette (two search
pages, bulk changelog pages, one remote-link call per issue), the real ``LakeWriter`` and
the T02-13 staging build (``120_stg_jira.sql``) on a temp DuckDB build through
``build_harness``, with ``mappings.custom_fields.jira`` set as the synth profile sets it.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.integration.model._stg_lake import build, cast_stats
from tests.support import jira_pages
from tests.support.build_harness import BuildHarness
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._http_data import bind_resilience
from tests.unit.connectors._jira_data import SINCE, UNTIL, Replay, cassette, connector

from herness.connectors.base import METADATA_FIELDS
from herness.connectors.jira import JIRA_FIELDS, JIRA_ISSUE_COLUMNS
from herness.connectors.jira_changelog import project_history, project_remote_link
from herness.core.resilience import ProcessState
from herness.model.settings import CustomFieldsConfig
from herness.store.lake import LakeWriter

pytestmark = pytest.mark.integration

UTC = datetime.UTC
ENUMS = {
    "jira.issue_type": {"Story": "story"},
    "jira.status_category_key": {"new": "todo", "indeterminate": "in_progress", "done": "done"},
    "jira.status_category": {
        "To Do": "todo",
        "In Progress": "in_progress",
        "In Review": "in_progress",
        "Done": "done",
    },
}
CUSTOM = {"jira": jira_pages.SYNTH_CUSTOM_FIELDS}
OBJECTS = ("issuetype", "parent", "project", "status", "components", "labels", "issuelinks")


def _compact(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _custom_ids() -> tuple[str, ...]:
    """The configured Jira custom field ids in `mappings.custom_fields.jira` order."""
    fields = CustomFieldsConfig.model_validate(CUSTOM).jira.model_dump()
    return tuple(str(v) for v in fields.values() if v)


def _search_issues() -> list[dict[str, Any]]:
    """The issues of both search pages of the cassette, as the source returned them."""
    pages = [i for i in cassette("cloud_sync_it.json") if i["request"]["path"].endswith("/jql")]
    return [issue for page in pages for issue in page["response"]["json"]["issues"]]


def _sync(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState, raw: Path
) -> tuple[pa.Table, Replay]:
    """Sync the cassette through the connector into one committed lake file set."""
    del ops_store
    reset_process_state.sleep = lambda _s: None
    bind_resilience()
    replay = Replay(cassette("cloud_sync_it.json"))
    conn = connector(replay, custom=_custom_ids(), fetch_remote_links=True)
    with LakeWriter("jira", "issue", root=raw) as writer:
        for batch in conn.sync("issue", SINCE, UNTIL):
            writer.write(batch)
        files = list(writer.commit().files)
    return pa.concat_tables([pq.read_table(f) for f in files]), replay


def test_it01_11_cloud_sync_lands_the_raw_contract_read_by_staging(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState, build_harness: BuildHarness
) -> None:
    """IT01-11 the Cloud cassette synced through `JiraConnector` with a real `LakeWriter`:
    every §4.5 column is present with its encoding; staging builds `stg.jira_issue`,
    `stg.jira_transition` and `stg.jira_link` with the expected rows and `stg.cast_stats`
    reports no failed casts for the contract columns."""
    assert _custom_ids() == jira_pages.SYNTH_FIELD_IDS
    raw = build_harness.layout.raw
    assert raw == ops_store.data_root / "raw"
    table, replay = _sync(ops_store, reset_process_state, raw)
    assert not replay.interactions  # every search, bulk and remote-link page was read
    _check_lake(table)
    build(build_harness, enums=ENUMS, custom_fields=CUSTOM)
    _check_issues(build_harness)
    _check_transitions_and_links(build_harness)
    stats = cast_stats(build_harness, "jira_issue")
    assert stats == {
        "type": (4, 0),
        "status_category": (4, 0),
        "created_at": (4, 0),
        "resolved_at": (1, 0),
        "story_points": (4, 0),
        "estimate_cost_usd": (4, 0),
    }
    assert all(failed == 0 for _, failed in stats.values())


def _check_lake(table: pa.Table) -> None:
    """§4.5: the raw columns, their Arrow types and encodings."""
    assert table.column_names == [*METADATA_FIELDS, *JIRA_ISSUE_COLUMNS, *_custom_ids()]
    for name in (*JIRA_ISSUE_COLUMNS, *_custom_ids()):
        assert table.schema.field(name).type == pa.string(), name
    rows = table.sort_by("id").to_pylist()
    issues = _search_issues()
    assert (
        [r["id"] for r in rows] == [i["id"] for i in issues] == ["10001", "10002", "10003", "10004"]
    )
    for row, issue in zip(rows, issues, strict=True):
        fields = issue["fields"]
        n = int(issue["id"]) - 10000
        assert (row["_source"], row["_entity"], row["_deleted"]) == ("jira", "issue", False)
        assert row["_source_key"] == issue["id"]
        assert row["_record_id"] == f"jira:issue:{issue['id']}"
        updated = datetime.datetime.strptime(fields["updated"], "%Y-%m-%dT%H:%M:%S.%f%z")
        assert row["_source_updated_at"] == updated
        assert json.loads(row["_payload"]) == issue
        assert row["_payload"] == _compact(issue)
        assert row["key"] == issue["key"]
        for name in OBJECTS:
            assert row[name] == _compact(fields[name]), name
        for name in ("created", "resolutiondate", "updated", "summary"):
            assert row[name] == fields[name], name
        assert row["description"] == _compact(fields["description"])  # Cloud: ADF as JSON
        assert row["customfield_10016"] == f"{float(n)}"
        assert row["customfield_10050"] == f"{1000.0 * n}"
        assert row["customfield_10060"] == "Synthetic Team"
        changelog = json.loads(row["changelog"])
        assert row["changelog"] == _compact(changelog)
        assert changelog == sorted(changelog, key=lambda h: (h["created"], h["id"]))
        assert all(set(h) == {"id", "created", "items"} for h in changelog)  # no author
    assert set(JIRA_FIELDS) <= set(table.column_names)

    by_id = {r["id"]: r for r in rows}
    one = [project_history(h) for h in jira_pages.histories(1, 3)]
    assert json.loads(by_id["10001"]["changelog"]) == one  # split over two bulk pages
    links = [project_remote_link(jira_pages.remote_link(3, k)) for k in range(2)]
    assert by_id["10003"]["remotelinks"] == _compact(links)
    assert by_id["10002"]["remotelinks"] == "[]"


def _check_issues(build_harness: BuildHarness) -> None:
    """T02-13 `stg.jira_issue`: one typed row per issue, custom fields of the synth profile."""
    staged = build_harness.query(
        'SELECT record_id, "key", type, parent_key, project, status, status_category,'
        " created_at, resolved_at, story_points, estimate_cost_usd, team_value, summary"
        ' FROM stg.jira_issue ORDER BY "key"'
    )
    created = datetime.datetime(2026, 8, 1, 9, tzinfo=UTC)
    resolved = datetime.datetime(2026, 9, 1, 9, 30, tzinfo=UTC)
    assert staged == [
        (
            f"jira:issue:1000{n}",
            f"SYN-{n}",
            "story",
            "SYN-0",
            "SYN",
            "In Progress" if n == 4 else "Done",
            "in_progress" if n == 4 else "done",
            created,
            resolved if n == 2 else None,
            float(n),
            Decimal(f"{1000 * n}.00"),
            "Synthetic Team",
            f"Synthetic issue {n}",
        )
        for n in (1, 2, 3, 4)
    ]


def _check_transitions_and_links(build_harness: BuildHarness) -> None:
    """T02-13 `stg.jira_transition` (status items of the complete changelog) and
    `stg.jira_link` (issue links and ticket mentions in the remote links)."""

    def at(k: int) -> datetime.datetime:
        return datetime.datetime(2026, 8, 1, 10, k, tzinfo=UTC)

    transitions = build_harness.query(
        'SELECT record_id, "at", from_status, to_status, from_category, to_category'
        ' FROM stg.jira_transition ORDER BY record_id, "at"'
    )
    walk = [
        ("To Do", "In Progress", "todo", "in_progress"),
        ("In Progress", "In Review", "in_progress", "in_progress"),
        ("In Review", "Done", "in_progress", "done"),
    ]
    assert transitions == [
        ("jira:issue:10001", at(0), *walk[0]),
        ("jira:issue:10001", at(1), *walk[1]),
        ("jira:issue:10001", at(2), *walk[2]),
        ("jira:issue:10002", at(0), *walk[0]),
        ("jira:issue:10002", at(1), *walk[1]),
        ("jira:issue:10004", at(0), *walk[0]),
    ]  # issue 3's only history changes `labels`, not `status`

    assert build_harness.query(
        "SELECT from_key, to_key, link_type FROM stg.jira_link ORDER BY ALL"
    ) == [
        ("SYN-1", "INC0012350", "mentions_incident"),
        ("SYN-2", "SYN-1", "Blocks"),
        ("SYN-3", "INC0012370", "mentions_incident"),
        ("SYN-3", "INC0012371", "mentions_incident"),
    ]
