"""Jira staging `120_stg_jira.sql` (impl 02 U02-110) under the card's IDs IT02-02 … IT02-08.

Issue rows carry the §4.1.2 raw columns (objects and arrays as JSON text) and are written
with the real `LakeWriter`; each test builds files 000-199 through `build_harness`.
"""

import datetime
import json
from decimal import Decimal

import pytest
from tests.integration.model._stg_lake import UTC, Row, at, build, cast_stats, columns, commit
from tests.support.build_harness import BuildHarness

pytestmark = pytest.mark.integration

ENUMS = {
    "jira.issue_type": {"Story": "story", "Bug": "bug", "Epic": "epic"},
    "jira.status_category_key": {"new": "todo", "indeterminate": "in_progress", "done": "done"},
    "jira.status_category": {"To Do": "todo", "In Progress": "in_progress", "Done": "done"},
}
JIRA_TABLES = ("jira_issue", "jira_transition", "jira_link")


def _j(value: object) -> str:
    return json.dumps(value)


def _status(name: str, key: str | None = "indeterminate") -> str:
    category = {"key": key} if key is not None else {}
    return _j({"name": name, "statusCategory": category})


def _issue(key: str, **fields: str | None) -> dict[str, str | None]:
    base: dict[str, str | None] = {
        "key": key,
        "issuetype": _j({"name": "Story"}),
        "project": _j({"key": key.split("-", maxsplit=1)[0]}),
        "status": _status("In Progress"),
        "created": "2024-03-01T09:00:00.000+0000",
        "summary": f"summary of {key}",
    }
    base.update(fields)
    return base


def _history(created: str, *items: tuple[str, str, str]) -> dict[str, object]:
    return {
        "created": created,
        "items": [{"field": f, "fromString": a, "toString": b} for f, a, b in items],
    }


def test_it02_02_latest_jira_issue_wins(build_harness: BuildHarness) -> None:
    """IT02-02 three versions and an identical re-emit of an issue: the latest wins, one
    row, typed per U02-110 (type, parent, project, components, labels, status, text)."""
    raw = build_harness.layout.raw
    v1 = _issue("PAY-1", status=_status("To Do", "new"))
    v2 = _issue("PAY-1", status=_status("In Progress"))
    adf = {
        "type": "doc",
        "content": [
            {"type": "paragraph", "content": [{"type": "text", "text": 'Card "declined"'}]}
        ],
    }
    v3 = _issue(
        "PAY-1",
        issuetype=_j({"name": "Bug"}),
        parent=_j({"key": "PAY-0"}),
        components=_j([{"name": "api"}, {"name": "web"}]),
        labels=_j(["p1", "checkout"]),
        status=_status("Done", "done"),
        resolutiondate="2024-03-02T10:30:00.000+0000",
        description=_j(adf),
    )
    commit(raw, "jira", "issue", [Row("10001", at(0), v1)])
    commit(raw, "jira", "issue", [Row("10001", at(1), v2)])
    commit(raw, "jira", "issue", [Row("10001", at(2), v3)])
    commit(raw, "jira", "issue", [Row("10001", at(2), v3, fetched=at(7))])
    build(build_harness, enums=ENUMS)
    rows = build_harness.query(
        'SELECT record_id, source_key, "key", type, parent_key, project, components, labels,'
        " status, status_category, created_at, resolved_at, story_points, estimate_cost_usd,"
        " team_value, summary, description FROM stg.jira_issue"
    )
    assert rows == [
        (
            "jira:issue:10001",
            "10001",
            "PAY-1",
            "bug",
            "PAY-0",
            "PAY",
            ["api", "web"],
            ["p1", "checkout"],
            "Done",
            "done",
            datetime.datetime(2024, 3, 1, 9, tzinfo=UTC),
            datetime.datetime(2024, 3, 2, 10, 30, tzinfo=UTC),
            None,
            None,
            None,
            "summary of PAY-1",
            'Card "declined"',
        )
    ]
    assert cast_stats(build_harness, "jira_issue") == {
        "type": (1, 0),
        "status_category": (1, 0),
        "created_at": (1, 0),
        "resolved_at": (1, 0),
    }
    build(build_harness, enums=ENUMS, lo=120, hi=120)  # re-run: idempotent
    assert cast_stats(build_harness, "jira_issue")["type"] == (1, 0)


def test_it02_02_transitions_from_latest_changelog(build_harness: BuildHarness) -> None:
    """IT02-02 `stg.jira_transition`: status items of the latest version's changelog, in
    both the array form and the object-with-`histories` form; other fields are skipped."""
    raw = build_harness.layout.raw
    old = _j([_history("2024-03-01T09:30:00.000+0000", ("status", "To Do", "Blocked"))])
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
        {
            "startAt": 0,
            "histories": [
                _history("2024-03-03T08:00:00.000+0000", ("status", "In Progress", "Review"))
            ],
        }
    )
    commit(raw, "jira", "issue", [Row("1", at(0), _issue("A-1", changelog=old))])
    commit(
        raw,
        "jira",
        "issue",
        [
            Row("1", at(1), _issue("A-1", changelog=array_form)),
            Row("2", at(1), _issue("A-2", changelog=object_form)),
            Row("3", at(1), _issue("A-3", changelog="not json")),
        ],
    )
    build(build_harness, enums=ENUMS)
    rows = build_harness.query(
        'SELECT record_id, "at", from_status, to_status, from_category, to_category'
        ' FROM stg.jira_transition ORDER BY record_id, "at"'
    )
    assert rows == [
        (
            "jira:issue:1",
            datetime.datetime(2024, 3, 1, 10, tzinfo=UTC),
            "To Do",
            "In Progress",
            "todo",
            "in_progress",
        ),
        (
            "jira:issue:1",
            datetime.datetime(2024, 3, 2, 10, tzinfo=UTC),
            "In Progress",
            "Done",
            "in_progress",
            "done",
        ),
        (
            "jira:issue:2",
            datetime.datetime(2024, 3, 3, 8, tzinfo=UTC),
            "In Progress",
            "Review",
            "in_progress",
            None,
        ),
    ]


def test_it02_02_links_distinct(build_harness: BuildHarness) -> None:
    """IT02-02 `stg.jira_link`: outward and inward issue links and ticket mentions in
    remote links of the latest version, DISTINCT."""
    raw = build_harness.layout.raw
    blocks = {"name": "Blocks"}
    links = _j(
        [
            {"type": blocks, "outwardIssue": {"key": "B-2"}},
            {"type": blocks, "outwardIssue": {"key": "B-2"}},
            {"type": {"name": "Relates"}, "inwardIssue": {"key": "C-3"}},
            {"type": blocks},
        ]
    )
    remote = _j(
        [
            {"object": {"url": "https://sn.example/nav?sys=INC0012345", "title": "INC0012345"}},
            {"object": {"url": "https://x.example", "title": "CHG0000042 and PRB1234 not INC12"}},
            {"object": {}},
        ]
    )
    commit(raw, "jira", "issue", [Row("1", at(0), _issue("A-1", issuelinks=_j([])))])
    commit(
        raw, "jira", "issue", [Row("1", at(1), _issue("A-1", issuelinks=links, remotelinks=remote))]
    )
    build(build_harness, enums=ENUMS)
    rows = build_harness.query("SELECT from_key, to_key, link_type FROM stg.jira_link ORDER BY ALL")
    assert rows == [
        ("A-1", "B-2", "Blocks"),
        ("A-1", "CHG0000042", "mentions_incident"),
        ("A-1", "INC0012345", "mentions_incident"),
        ("A-1", "PRB1234", "mentions_incident"),
        ("C-3", "A-1", "Relates"),
    ]


def test_it02_03_jira_tombstone(build_harness: BuildHarness) -> None:
    """IT02-03 a deleted issue (latest version a tombstone) leaves no issue, transition or
    link rows; an older tombstone followed by a live version is kept."""
    raw = build_harness.layout.raw
    changelog = _j([_history("2024-03-01T10:00:00.000+0000", ("status", "To Do", "Done"))])
    links = _j([{"type": {"name": "Blocks"}, "outwardIssue": {"key": "Z-9"}}])
    commit(
        raw,
        "jira",
        "issue",
        [
            Row("1", at(0), _issue("A-1", changelog=changelog, issuelinks=links)),
            Row("2", at(0), deleted=True),
        ],
    )
    commit(raw, "jira", "issue", [Row("1", at(1), deleted=True), Row("2", at(1), _issue("A-2"))])
    build(build_harness, enums=ENUMS)
    assert build_harness.query('SELECT "key" FROM stg.jira_issue') == [("A-2",)]
    assert build_harness.query("SELECT count(*) FROM stg.jira_transition") == [(0,)]
    assert build_harness.query("SELECT count(*) FROM stg.jira_link") == [(0,)]


def test_it02_04_jira_deleted_record(build_harness: BuildHarness) -> None:
    """IT02-04 an issue whose record ID is in `stg.deleted_record` (a `running`/`done`
    request) is not staged, nor are its transitions (TH02-16, staging part)."""
    raw = build_harness.layout.raw
    changelog = _j([_history("2024-03-01T10:00:00.000+0000", ("status", "To Do", "Done"))])
    rows = [Row(k, at(0), _issue(f"A-{k}", changelog=changelog)) for k in ("1", "2")]
    commit(raw, "jira", "issue", rows)
    build(build_harness, enums=ENUMS, deleted_ids=["jira:issue:1"])
    assert build_harness.query('SELECT "key" FROM stg.jira_issue') == [("A-2",)]
    assert build_harness.query("SELECT record_id FROM stg.jira_transition") == [("jira:issue:2",)]


def test_it02_05_jira_custom_fields_absent(build_harness: BuildHarness) -> None:
    """IT02-05 an unknown column and custom fields configured but absent from the lake:
    the build succeeds, the custom columns are NULL and counted as never non-null."""
    raw = build_harness.layout.raw
    commit(raw, "jira", "issue", [Row("1", at(0), _issue("A-1", customfield_99999="x"))])
    custom = {
        "jira": {
            "story_points": "customfield_10016",
            "team": "customfield_10001",
            "estimate_cost_usd": "customfield_10050",
            "epic_link": "customfield_10014",
        }
    }
    build(build_harness, enums=ENUMS, custom_fields=custom)
    rows = build_harness.query(
        "SELECT parent_key, story_points, estimate_cost_usd, team_value FROM stg.jira_issue"
    )
    assert rows == [(None, None, None, None)]
    stats = cast_stats(build_harness, "jira_issue")
    assert stats["story_points"] == (0, 0)
    assert stats["estimate_cost_usd"] == (0, 0)


def test_it02_05_jira_custom_fields_present(build_harness: BuildHarness) -> None:
    """IT02-05 configured custom fields present in the lake: story points, cost, team value
    and the epic link as parent fallback."""
    raw = build_harness.layout.raw
    rows = [
        Row(
            "1",
            at(0),
            _issue(
                "A-1",
                customfield_10016="5",
                customfield_10050="1234.567",
                customfield_10001=_j({"id": "7", "name": "Payments Team"}),
                customfield_10014="A-100",
            ),
        ),
        Row(
            "2",
            at(0),
            _issue(
                "A-2",
                parent=_j({"key": "A-50"}),
                customfield_10016="a lot",
                customfield_10050="cheap",
                customfield_10001="Core",
                customfield_10014="A-100",
            ),
        ),
    ]
    commit(raw, "jira", "issue", rows)
    custom = {
        "jira": {
            "story_points": "customfield_10016",
            "team": "customfield_10001",
            "estimate_cost_usd": "customfield_10050",
            "epic_link": "customfield_10014",
        }
    }
    build(build_harness, enums=ENUMS, custom_fields=custom)
    got = build_harness.query(
        'SELECT "key", parent_key, story_points, estimate_cost_usd, team_value'
        ' FROM stg.jira_issue ORDER BY "key"'
    )
    assert got == [
        ("A-1", "A-100", 5.0, Decimal("1234.57"), "Payments Team"),
        ("A-2", "A-50", None, None, "Core"),
    ]
    stats = cast_stats(build_harness, "jira_issue")
    assert stats["story_points"] == (2, 1)
    assert stats["estimate_cost_usd"] == (2, 1)


def test_it02_07_jira_cast_stats(build_harness: BuildHarness) -> None:
    """IT02-07 3 bad `created` timestamps of 100: failed 3, non_null 100; the status
    category falls back to the status name and fails only when both miss."""
    bad = {3: "03/01/2024", 50: "1709283600000", 97: "yesterday"}
    statuses = {10: _status("Done", None), 11: _status("In Progress", "odd"), 12: _status("?", "x")}
    rows = [
        Row(
            str(i),
            at(0),
            _issue(
                f"A-{i}",
                created=bad.get(i, "2024-03-01T09:00:00.000+0000"),
                status=statuses.get(i, _status("In Progress")),
            ),
        )
        for i in range(100)
    ]
    commit(build_harness.layout.raw, "jira", "issue", rows)
    build(build_harness, enums=ENUMS)
    stats = cast_stats(build_harness, "jira_issue")
    assert stats["created_at"] == (100, 3)
    assert stats["resolved_at"] == (0, 0)
    assert stats["status_category"] == (100, 1)
    categories = build_harness.query(
        'SELECT "key", status_category FROM stg.jira_issue'
        " WHERE \"key\" IN ('A-10', 'A-11', 'A-12') ORDER BY 1"
    )
    assert categories == [("A-10", "done"), ("A-11", "in_progress"), ("A-12", None)]


def test_it02_08_jira_issue_absent(build_harness: BuildHarness) -> None:
    """IT02-08 no `jira/issue` files: the build succeeds and the three Jira staging tables
    are empty with the columns and types of a populated build."""
    build(build_harness, enums=ENUMS)
    empty = {t: columns(build_harness, t) for t in JIRA_TABLES}
    for table in JIRA_TABLES:
        assert build_harness.query(f"SELECT count(*) FROM stg.{table}") == [(0,)]  # noqa: S608
    commit(build_harness.layout.raw, "jira", "issue", [Row("1", at(0), _issue("A-1"))])
    build(build_harness, enums=ENUMS)
    assert {t: columns(build_harness, t) for t in JIRA_TABLES} == empty
    assert ("components", "VARCHAR[]") in empty["jira_issue"]
    assert ("estimate_cost_usd", "DECIMAL(18,2)") in empty["jira_issue"]
    assert empty["jira_transition"] == [
        ("record_id", "VARCHAR"),
        ("at", "TIMESTAMP WITH TIME ZONE"),
        ("from_status", "VARCHAR"),
        ("to_status", "VARCHAR"),
        ("from_category", "VARCHAR"),
        ("to_category", "VARCHAR"),
    ]
    assert empty["jira_link"] == [
        ("from_key", "VARCHAR"),
        ("to_key", "VARCHAR"),
        ("link_type", "VARCHAR"),
    ]
