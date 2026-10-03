"""Tests for the Jira raw column contract: herness.connectors.jira.flatten_issue and the
herness.connectors.jira_changelog projections (impl 01 U01-93, U01-94; R-59; T01-17).

Also: importing ``flatten_issue`` (as the T11-12 generator does) pulls in no HTTP layer, and
the committed cassettes are exactly what ``tests.support.jira_pages`` regenerates (O-16).
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from tests.support import jira_pages
from tests.unit.connectors._jira_data import history

from herness.connectors.jira import JIRA_FIELDS, JIRA_ISSUE_COLUMNS, flatten_issue
from herness.connectors.jira_changelog import project_history, project_remote_link
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

CUSTOM = ["customfield_10016", "customfield_10014"]
LINKS = [
    {"id": 7, "self": "https://jira.example.test/x", "application": {"name": "wiki"},
     "relationship": "mentions", "object": {"url": "https://wiki.example.test/a",
     "title": "Runbook", "icon": {"url16x16": "https://wiki.example.test/i.png"}}},
    {"id": "8", "object": {"url": "https://wiki.example.test/b"}},
]  # fmt: skip


def _issue() -> dict[str, Any]:
    raw = jira_pages.issue(2)  # every JIRA_FIELDS member, ADF description, two custom fields
    raw["fields"]["issuelinks"] = [{"type": {"name": "Blocks"}, "outwardIssue": {"key": "SYN-9"}}]
    return raw


def _histories() -> list[dict[str, Any]]:
    """Histories with `author`, given out of order (created, then id)."""
    return [
        history("30", "2026-09-01T10:00:00.000+0000"),
        history("12", "2026-08-02T09:00:00.000+0000", items=[{"field": "labels"}, "x"]),
        history("11", "2026-08-02T09:00:00.000+0000"),
    ]


def test_ut01_96_keys_exact_order_and_values() -> None:
    """UT01-96 keys are exactly `JIRA_ISSUE_COLUMNS` + custom ids in order; objects are
    compact JSON, numbers text, timestamps the source strings; changelog sorted without
    `author`; remote links projected in input order."""
    out = flatten_issue(
        _issue(), changelog=_histories(), remotelinks=LINKS, custom_field_ids=CUSTOM
    )
    assert list(out) == [*JIRA_ISSUE_COLUMNS, *CUSTOM]
    assert ("id", "key", *JIRA_FIELDS, "changelog", "remotelinks") == JIRA_ISSUE_COLUMNS
    assert (out["id"], out["key"]) == ("10002", "SYN-2")
    assert all(v is None or isinstance(v, str) for v in out.values())
    adf = _issue()["fields"]["description"]
    assert out["description"] == json.dumps(adf, ensure_ascii=False, separators=(",", ":"))
    assert out["project"] == '{"id":"10100","key":"SYN","name":"Synthetic"}'
    assert out["labels"] == '["synthetic"]'
    assert out["updated"] == "2026-09-01T10:07:00.000+0000"
    assert out["resolutiondate"] == "2026-09-01T09:30:00.000+0000"
    assert (out["customfield_10016"], out["customfield_10014"]) == ("2.0", "SYN-0")
    changelog = json.loads(out["changelog"] or "")
    assert [h["id"] for h in changelog] == ["11", "12", "30"]
    assert all(set(h) == {"id", "created", "items"} for h in changelog)
    assert changelog[1]["items"] == [
        {"field": "labels", "from": None, "fromString": None, "to": None, "toString": None}
    ]
    assert "," + " " not in (out["changelog"] or "")
    assert json.loads(out["remotelinks"] or "") == [
        {"id": "7", "object": {"url": "https://wiki.example.test/a", "title": "Runbook"}},
        {"id": "8", "object": {"url": "https://wiki.example.test/b", "title": None}},
    ]


def test_ut01_96_absent_field_none_and_no_remote_links() -> None:
    """UT01-96 the same issue without `resolutiondate` (or without a custom field) keeps the
    key with `None`; `remotelinks=None` gives `None`; an empty changelog is `[]`."""
    raw = _issue()
    del raw["fields"]["resolutiondate"], raw["fields"]["customfield_10014"]
    out = flatten_issue(raw, changelog=[], remotelinks=None, custom_field_ids=CUSTOM)
    assert list(out) == [*JIRA_ISSUE_COLUMNS, *CUSTOM]
    assert (out["resolutiondate"], out["customfield_10014"]) == (None, None)
    assert (out["remotelinks"], out["changelog"]) == (None, "[]")
    assert list(flatten_issue(raw, changelog=[], remotelinks=None)) == list(JIRA_ISSUE_COLUMNS)


def test_ut01_96_projections_idempotent_and_minimal() -> None:
    """UT01-96 projecting a projected value returns an equal value; no `author`,
    `application` or `relationship` survives; `items` absent gives `[]`."""
    for raw in [*_histories(), {"id": "1", "created": "2026-01-01T00:00:00.000+0000"}]:
        once = project_history(raw)
        assert project_history(once) == once
        assert set(once) == {"id", "created", "items"}
    assert project_history({"id": "1", "created": "c"})["items"] == []
    for link in LINKS:
        once = project_remote_link(link)
        assert project_remote_link(once) == once
        assert set(once) == {"id", "object"}
        assert set(once["object"]) == {"url", "title"}  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "bad",
    [
        {"created": "c"},
        {"id": "1"},
        {"id": 1, "created": "c"},
        {"id": "1", "created": "c", "items": "x"},
    ],
)
def test_ut01_96_bad_history_is_schema_violation(bad: dict[str, Any]) -> None:
    """UT01-96 a history without string `id`/`created` (or with non-list items) is refused."""
    with pytest.raises(SchemaViolation, match=r"^bad changelog history$") as info:
        project_history(bad)
    assert info.value.context == {"source": "jira"}


@pytest.mark.parametrize(
    "bad", [{"object": {}}, {"id": 1}, {"id": True, "object": {}}, {"id": "1", "object": []}]
)
def test_ut01_96_bad_remote_link_is_schema_violation(bad: dict[str, Any]) -> None:
    """UT01-96 a link without `id` (number or string) or `object` mapping is refused."""
    with pytest.raises(SchemaViolation, match=r"^bad remote link$") as info:
        project_remote_link(bad)
    assert info.value.context == {"source": "jira"}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"id": "12a"}, "bad issue id"),
        ({"id": ""}, "bad issue id"),
        ({"id": 10002}, "bad issue id"),
        ({"id": "١٢"}, "bad issue id"),  # non-ASCII digits
        ({"key": "syn-2"}, "bad issue key"),
        ({"key": "SYN-"}, "bad issue key"),
        ({"fields": None}, "issue fields missing"),
    ],
)
def test_ut01_96_bad_issue_is_schema_violation(change: dict[str, Any], message: str) -> None:
    """UT01-96 `id` `"12a"`, a bad key or missing fields raise SchemaViolation without the
    issue's text in the message."""
    raw = _issue() | change
    with pytest.raises(SchemaViolation, match=message) as info:
        flatten_issue(raw, changelog=[], remotelinks=None)
    assert "Synthetic issue" not in str(info.value)


def test_ut01_96_bad_custom_id_is_config_error() -> None:
    """UT01-96 custom id `cf_1` raises ConfigError naming it."""
    with pytest.raises(ConfigError, match="bad custom field id cf_1"):
        flatten_issue(_issue(), changelog=[], remotelinks=None, custom_field_ids=["cf_1"])


def test_ut01_96_duplicate_custom_id_is_collision() -> None:
    """UT01-96 a custom id given twice is a `flatten_record` column collision."""
    with pytest.raises(SchemaViolation, match="column collision"):
        flatten_issue(_issue(), changelog=[], remotelinks=None, custom_field_ids=CUSTOM * 2)


def test_ut01_96_input_not_mutated() -> None:
    """UT01-96 flatten_issue is pure: the issue, histories and links are left unchanged."""
    raw, hist, links = _issue(), _histories(), copy.deepcopy(LINKS)
    before = copy.deepcopy((raw, hist, links))
    flatten_issue(raw, changelog=hist, remotelinks=links, custom_field_ids=CUSTOM)
    assert (raw, hist, links) == before


_PROBE = """\
import sys
from herness.connectors.jira import flatten_issue
heavy = sorted(m for m in ("httpx", "httpx2", "herness.connectors.http") if m in sys.modules)
sys.stdout.write("loaded:" + ",".join(heavy))
"""


def test_ut01_96_flatten_issue_imports_without_http_layer() -> None:
    """UT01-96 (T11-12 acceptance) a fresh interpreter importing
    `herness.connectors.jira.flatten_issue` imports neither `httpx`/`httpx2` nor the
    connectors HTTP layer, so no client can be built."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("HERNESS_")}
    done = subprocess.run(  # noqa: S603 - fixed interpreter and script, test only
        [sys.executable, "-c", _PROBE],
        capture_output=True,
        text=True,
        check=False,
        env=env | {"HERNESS_ENV": "test"},
        cwd=Path(__file__).resolve().parents[3],
        timeout=120,
    )
    assert done.returncode == 0, done.stderr[-2000:]
    assert done.stdout.endswith("loaded:")


def test_ut01_96_cassettes_regenerate_byte_for_byte(tmp_path: Path) -> None:
    """UT01-96 (O-16) regenerating the Jira cassettes reproduces the committed files byte
    for byte; hosts are synthetic and every token starts with `synthetic` (R-67)."""
    written = jira_pages.write_jira_pages(tmp_path)
    committed = sorted(p.name for p in jira_pages.CASSETTE_DIR.iterdir())
    assert sorted(p.name for p in written) == committed
    for path in written:
        assert path.read_bytes() == (jira_pages.CASSETTE_DIR / path.name).read_bytes(), path.name
        text = path.read_text(encoding="utf-8")
        assert "atlassian.net" not in text
        for page in json.loads(text)["interactions"]:
            body = page["response"]["json"]
            token = body.get("nextPageToken") if isinstance(body, dict) else None
            assert token is None or token.startswith("synthetic")
