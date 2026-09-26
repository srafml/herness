"""`280_work_item.sql` (U02-123) on top of Jira staging `120` (U02-110), impl 02 T02-17.

IT02-18: `core.work_item` per U02-123 (Cloud-style parent, DC epic link, components,
custom fields, service by delivery mapping, team by mapping or team name). IT02-19:
`core.work_item_transition` from both changelog forms, only for issues in `core.work_item`.
IT02-20: `core.work_item_link` from inward/outward issue links and `mentions_incident`
remote links, DISTINCT. Earlier Jira staging tests keep their IT02-02…08 IDs.
"""

from __future__ import annotations

import datetime
import json
from decimal import Decimal

import duckdb
import pytest
from tests.integration.model._core_build import SVC, TEAM
from tests.integration.model._core_late import build_late, rerun
from tests.integration.model._stg_lake import UTC, Row, at, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

ENUMS = {
    "jira.issue_type": {"Story": "story", "Bug": "bug", "Epic": "epic"},
    "jira.status_category_key": {"new": "todo", "indeterminate": "in_progress", "done": "done"},
    "jira.status_category": {"To Do": "todo", "In Progress": "in_progress", "Done": "done"},
}
CUSTOM = {
    "jira": {
        "story_points": "customfield_10016",
        "team": "customfield_10001",
        "estimate_cost_usd": "customfield_10050",
        "epic_link": "customfield_10014",
    }
}


def _delivery(
    service: str, project: str, component: str | None, team: str | None
) -> dict[str, str | None]:
    return {
        "service_id": SVC + service,
        "team_id": None if team is None else TEAM + team,
        "jira_project": project,
        "jira_component": component,
        "role": "delivery",
    }


MAPPINGS = {
    "enums": ENUMS,
    "service_overrides": [
        _delivery("s1", "PAY", "api", "t1"),
        _delivery("s2", "PAY", None, None),
        _delivery("s3", "WEB", "ui", None),
        _delivery("s6", "WEB", None, None),  # not used: (WEB, ui) matches (twice)
        _delivery("s5", "OPS", None, "t2"),
    ],
}
ISSUE = "jira:issue:"


def _j(value: object) -> str:
    return json.dumps(value)


def _issue(key: str, **fields: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "key": key,
        "issuetype": _j({"name": "Story"}),
        "project": _j({"key": key.split("-", maxsplit=1)[0]}),
        "status": _j({"name": "In Progress", "statusCategory": {"key": "indeterminate"}}),
        "created": "2024-03-01T09:00:00.000+0000",
        "summary": f"summary of {key}",
    }
    base.update(fields)
    return base


def _teams(harness: BuildHarness) -> None:
    groups = [("t1", "T1"), ("t2", "T2"), ("pay", "Payments Team"), ("d1", "Dup"), ("d2", "dup")]
    commit(
        harness.layout.raw,
        "servicenow",
        "sys_user_group",
        [Row(g, at(0), {"sys_id": g, "name": name}) for g, name in groups],
    )


def _second_web_ui(con: duckdb.DuckDBPyConnection) -> None:
    """A second delivery row for (WEB, ui): the service mapping becomes ambiguous."""
    con.execute(
        "INSERT INTO core.service_map (service_id, jira_project, jira_component, role)"
        " VALUES (?, 'WEB', 'ui', 'delivery')",
        [SVC + "s4"],
    )


def _comps(*names: str) -> str:
    return _j([{"name": n} for n in names])


def test_it02_18_work_item_rules(build_harness: BuildHarness) -> None:
    """IT02-18 Cloud-style parent, DC epic link, components and custom fields: staging
    columns carried over, `component` = first component, service by (project, component)
    then (project, NULL), team by the matched mapping row else the single team by name."""
    _teams(build_harness)
    issues = [
        Row(
            "1",
            at(0),
            _issue(
                "PAY-1",
                issuetype=_j({"name": "Bug"}),
                parent=_j({"key": "PAY-0"}),
                components=_comps("api", "web"),
                labels=_j(["p1"]),
                status=_j({"name": "Done", "statusCategory": {"key": "done"}}),
                resolutiondate="2024-03-02T10:00:00.000+0000",
                customfield_10016="3",
                customfield_10050="10.5",
                customfield_10001="Payments Team",
            ),
        ),
        Row(
            "2",
            at(1),
            _issue(
                "PAY-2",
                components=_comps("web"),
                customfield_10014="PAY-100",
                customfield_10001=_j({"id": "9", "name": "payments team"}),
            ),
        ),
        Row("3", at(0), _issue("WEB-1", components=_comps("ui"), customfield_10001="t1")),
        Row("4", at(0), _issue("OPS-1", customfield_10001="Payments Team")),
        Row("5", at(0), _issue("NEW-1", components=_comps("x"), customfield_10001="DUP")),
    ]
    commit(build_harness.layout.raw, "jira", "issue", issues)
    build_late(build_harness, [280], mappings=MAPPINGS, custom_fields=CUSTOM, setup=_second_web_ui)
    shape_sql = (
        'SELECT record_id, "key", type, parent_key, project, component, components, labels'
        " FROM core.work_item ORDER BY record_id"
    )
    shape = build_harness.query(shape_sql)
    assert shape == [
        (ISSUE + "1", "PAY-1", "bug", "PAY-0", "PAY", "api", ["api", "web"], ["p1"]),
        (ISSUE + "2", "PAY-2", "story", "PAY-100", "PAY", "web", ["web"], None),  # epic link
        (ISSUE + "3", "WEB-1", "story", None, "WEB", "ui", ["ui"], None),
        (ISSUE + "4", "OPS-1", "story", None, "OPS", None, None, None),
        (ISSUE + "5", "NEW-1", "story", None, "NEW", "x", ["x"], None),
    ]
    mapped_sql = 'SELECT "key", team_id, service_id FROM core.work_item ORDER BY record_id'
    mapped = build_harness.query(mapped_sql)
    assert mapped == [
        ("PAY-1", TEAM + "t1", SVC + "s1"),  # (PAY, api); team of the mapping row
        ("PAY-2", TEAM + "pay", SVC + "s2"),  # (PAY, NULL); no mapping team: team by name
        ("WEB-1", TEAM + "t1", None),  # two services for (WEB, ui): none; team by name
        ("OPS-1", TEAM + "t2", SVC + "s5"),  # no component: (OPS, NULL)
        ("NEW-1", None, None),  # unmapped; team name matches two teams
    ]
    done = datetime.datetime(2024, 3, 2, 10, tzinfo=UTC)
    created = datetime.datetime(2024, 3, 1, 9, tzinfo=UTC)
    values = build_harness.query(
        'SELECT "key", status, status_category, created_at, resolved_at, source_updated_at,'
        " story_points, estimate_cost_usd FROM core.work_item"
        " WHERE record_id IN (?, ?) ORDER BY record_id",
        [ISSUE + "1", ISSUE + "2"],
    )
    assert values == [
        ("PAY-1", "Done", "done", created, done, at(0), 3.0, Decimal("10.50")),
        ("PAY-2", "In Progress", "in_progress", created, None, at(1), None, None),
    ]
    texts = build_harness.query('SELECT "key", summary, description FROM core.work_item')
    assert ("PAY-1", "summary of PAY-1", None) in texts
    rerun(build_harness, 280)  # idempotent
    assert build_harness.query(shape_sql) == shape
    assert build_harness.query(mapped_sql) == mapped


def test_it02_18_work_item_empty_typed(build_harness: BuildHarness) -> None:
    """IT02-18 no Jira files: the three work item tables exist, empty, with U02-123 types."""
    build_late(build_harness, [280])
    types = build_harness.query(
        "SELECT table_name, column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'core' AND table_name LIKE 'work_item%'"
        " ORDER BY table_name, ordinal_position"
    )
    by_table: dict[str, dict[str, str]] = {}
    for table, name, kind in types:
        by_table.setdefault(str(table), {})[str(name)] = str(kind)
    for table in by_table:
        assert build_harness.query(f"SELECT count(*) FROM core.{table}") == [(0,)]  # noqa: S608
    item = by_table["work_item"]
    assert item["components"] == item["labels"] == "VARCHAR[]"
    assert item["created_at"] == item["resolved_at"] == "TIMESTAMP WITH TIME ZONE"
    assert item["source_updated_at"] == "TIMESTAMP WITH TIME ZONE"
    assert item["story_points"] == "DOUBLE"
    assert item["estimate_cost_usd"] == "DECIMAL(18,2)"
    assert item["component"] == item["team_id"] == item["service_id"] == "VARCHAR"
    assert set(item) == {
        "record_id", "key", "type", "parent_key", "project", "component", "components",
        "labels", "status", "status_category", "created_at", "resolved_at", "story_points",
        "estimate_cost_usd", "team_id", "service_id", "summary", "description",
        "source_updated_at",
    }  # fmt: skip
    assert by_table["work_item_transition"] == {
        "record_id": "VARCHAR",
        "from_status": "VARCHAR",
        "to_status": "VARCHAR",
        "from_category": "VARCHAR",
        "to_category": "VARCHAR",
        "at": "TIMESTAMP WITH TIME ZONE",
    }
    assert by_table["work_item_link"] == {
        "from_key": "VARCHAR",
        "to_key": "VARCHAR",
        "link_type": "VARCHAR",
    }


def _history(created: str, *items: tuple[str, str, str]) -> dict[str, object]:
    return {
        "created": created,
        "items": [{"field": f, "fromString": a, "toString": b} for f, a, b in items],
    }


def _orphan_transition(con: duckdb.DuckDBPyConnection) -> None:
    """A staged transition whose issue is not in `core.work_item`."""
    con.execute(
        "INSERT INTO stg.jira_transition VALUES"
        " ('jira:issue:999', TIMESTAMPTZ '2024-03-01 00:00:00+00', 'To Do', 'Done', 'todo', 'done')"
    )


def test_it02_19_transitions_with_categories(build_harness: BuildHarness) -> None:
    """IT02-19 changelog in array and object forms: one transition per status item with
    its categories; a staged transition of an issue not in `core.work_item` is left out."""
    array_form = _j(
        [
            _history("2024-03-01T10:00:00.000+0000", ("status", "To Do", "In Progress")),
            _history(
                "2024-03-02T10:00:00.000+0000",
                ("assignee", "a", "b"),
                ("status", "In Progress", "Done"),
            ),
        ]
    )
    object_form = _j(
        {"histories": [_history("2024-03-03T08:00:00.000+0000", ("status", "Done", "Review"))]}
    )
    issues = [
        Row("1", at(0), _issue("A-1", changelog=array_form)),
        Row("2", at(0), _issue("A-2", changelog=object_form)),
    ]
    commit(build_harness.layout.raw, "jira", "issue", issues)
    build_late(build_harness, [280], mappings={"enums": ENUMS}, setup=_orphan_transition)
    rows = build_harness.query(
        'SELECT record_id, "at", from_status, to_status, from_category, to_category'
        ' FROM core.work_item_transition ORDER BY record_id, "at"'
    )

    def _at(day: int, hour: int) -> datetime.datetime:
        return datetime.datetime(2024, 3, day, hour, tzinfo=UTC)

    assert rows == [
        (ISSUE + "1", _at(1, 10), "To Do", "In Progress", "todo", "in_progress"),
        (ISSUE + "1", _at(2, 10), "In Progress", "Done", "in_progress", "done"),
        (ISSUE + "2", _at(3, 8), "Done", "Review", "done", None),
    ]


def _duplicate_link(con: duckdb.DuckDBPyConnection) -> None:
    """A second copy of a staged link row: the core table stays DISTINCT."""
    con.execute("INSERT INTO stg.jira_link SELECT * FROM stg.jira_link WHERE to_key = 'B-2'")


def test_it02_20_links_with_mentions(build_harness: BuildHarness) -> None:
    """IT02-20 issuelinks inward/outward and remotelinks with INC numbers: one row per
    distinct (from_key, to_key, link_type), including `mentions_incident`."""
    links = _j(
        [
            {"type": {"name": "Blocks"}, "outwardIssue": {"key": "B-2"}},
            {"type": {"name": "Relates"}, "inwardIssue": {"key": "C-3"}},
        ]
    )
    remote = _j(
        [
            {"object": {"url": "https://sn.example/nav?INC0012345", "title": "INC0012345"}},
            {"object": {"title": "see INC0000777 and CHG0000042"}},
        ]
    )
    issues = [Row("1", at(0), _issue("A-1", issuelinks=links, remotelinks=remote))]
    commit(build_harness.layout.raw, "jira", "issue", issues)
    build_late(build_harness, [280], mappings={"enums": ENUMS}, setup=_duplicate_link)
    rows = build_harness.query(
        "SELECT from_key, to_key, link_type FROM core.work_item_link ORDER BY ALL"
    )
    assert rows == [
        ("A-1", "B-2", "Blocks"),
        ("A-1", "CHG0000042", "mentions_incident"),
        ("A-1", "INC0000777", "mentions_incident"),
        ("A-1", "INC0012345", "mentions_incident"),
        ("C-3", "A-1", "Relates"),
    ]
