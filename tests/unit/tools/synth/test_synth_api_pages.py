"""Tests for tools.synth.api_pages (U11-26): UT11-67."""

import json
import re
from pathlib import Path
from typing import Any

import pytest

from tools.synth.api_pages import write_api_pages
from tools.synth.params import SynthUsageError

pytestmark = pytest.mark.unit

_SN_LINK = re.compile(
    r"<https://synthetic-instance\.example\.com/api/now/table/incident"
    r'\?sysparm_limit=1000&sysparm_offset=(\d+)>;rel="next"'
)
_PII = re.compile(r"example\.(com|org)|555[- ]01\d\d|synthetic[0-9a-zA-Z]|192\.0\.2\.|4111\d{12}")


def _load(out: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    manifest = json.loads((out / "manifest.json").read_text(encoding="utf-8"))
    bodies = [json.loads((out / p["file"]).read_text(encoding="utf-8")) for p in manifest["pages"]]
    return manifest, bodies


@pytest.fixture(scope="module")
def sn_pages(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], list[Any]]:
    out = tmp_path_factory.mktemp("sn") / "pages"
    assert write_api_pages(3, "servicenow", "incident", 2500, out) == 3
    return _load(out)


@pytest.fixture(scope="module")
def jira_pages(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], list[Any]]:
    out = tmp_path_factory.mktemp("jira") / "pages"
    assert write_api_pages(3, "jira", "issue", 250, out) == 3  # page size capped at 100
    return _load(out)


def test_ut11_67_servicenow_pages_link_on_all_but_the_last(
    sn_pages: tuple[dict[str, Any], list[Any]],
) -> None:
    """UT11-67 2,500 ServiceNow incident rows: 3 pages; `Link: rel="next"` on the first
    two naming the next offset, none on the last; the manifest lists files, rows, headers
    and the Table API request of each page."""
    manifest, bodies = sn_pages
    assert {k: manifest[k] for k in ("source", "entity", "rows")} == {
        "source": "servicenow",
        "entity": "incident",
        "rows": 2500,
    }
    pages = manifest["pages"]
    assert [p["file"] for p in pages] == ["page-00001.json", "page-00002.json", "page-00003.json"]
    assert [p["rows"] for p in pages] == [1000, 1000, 500]
    assert [len(b["result"]) for b in bodies] == [1000, 1000, 500]
    offsets = [_SN_LINK.fullmatch(p["headers"]["Link"]) for p in pages[:2]]
    assert [m.group(1) if m else None for m in offsets] == ["1000", "2000"]
    assert pages[2]["headers"] == {}
    for page, offset in zip(pages, (0, 1000, 2000), strict=True):
        assert page["request"] == {
            "method": "GET",
            "path": "/api/now/table/incident",
            "params": {"sysparm_limit": "1000", "sysparm_offset": str(offset)},
        }


def test_ut11_67_servicenow_records_are_pairs_in_listing_order(
    sn_pages: tuple[dict[str, Any], list[Any]],
) -> None:
    """UT11-67 records are `{value, display_value}` pairs; numbers and keys are unique;
    `sys_updated_on` ascends across pages (one `ORDERBYsys_updated_on` listing) and
    follows every other timestamp of the record; no PII (rate 0)."""
    _, bodies = sn_pages
    records = [r for b in bodies for r in b["result"]]
    for record in records:
        for field in record.values():
            assert set(field) == {"value", "display_value"}
    keys = [r["sys_id"]["value"] for r in records]
    numbers = [r["number"]["value"] for r in records]
    stamps = [r["sys_updated_on"]["value"] for r in records]
    assert len(set(keys)) == len(set(numbers)) == 2500
    assert stamps == sorted(stamps)
    assert len(set(stamps)) == 2500
    for record in records:
        for name in ("opened_at", "resolved_at", "closed_at"):
            assert record[name]["value"] < record["sys_updated_on"]["value"]
        assert not _PII.search(record["description"]["value"])


def test_ut11_67_jira_pages_is_last_only_on_the_last(
    jira_pages: tuple[dict[str, Any], list[Any]],
) -> None:
    """UT11-67 250 Jira rows (page size capped at 100): 3 Cloud search pages; `isLast` is
    true only on the last; `nextPageToken` `synthetic-page-<n>` names the next page and
    is absent on the last; each request carries the token of its page."""
    manifest, bodies = jira_pages
    assert (manifest["source"], manifest["entity"], manifest["rows"]) == ("jira", "issue", 250)
    assert [p["rows"] for p in manifest["pages"]] == [100, 100, 50]
    assert [b["isLast"] for b in bodies] == [False, False, True]
    assert [b.get("nextPageToken") for b in bodies] == [
        "synthetic-page-2",
        "synthetic-page-3",
        None,
    ]
    requests = [p["request"] for p in manifest["pages"]]
    assert {(r["method"], r["path"]) for r in requests} == {("POST", "/rest/api/3/search/jql")}
    assert [r["json"].get("nextPageToken") for r in requests] == [
        None,
        "synthetic-page-2",
        "synthetic-page-3",
    ]
    assert all(r["json"]["maxResults"] == 100 for r in requests)
    issues = [i for b in bodies for i in b["issues"]]
    assert len({i["id"] for i in issues}) == len({i["key"] for i in issues}) == 250
    assert all(set(i) == {"id", "key", "fields"} for i in issues)
    updated = [i["fields"]["updated"] for i in issues]
    assert updated == sorted(updated)


@pytest.mark.parametrize("entity", ["change_request", "problem"])
def test_ut11_67_other_servicenow_entities(tmp_path: Path, entity: str) -> None:
    """UT11-67 change_request and problem pages carry `CHG` / `PRB` numbers; one page."""
    out = tmp_path / "pages"
    assert write_api_pages(3, "servicenow", entity, 7, out) == 1
    manifest, (body,) = _load(out)
    assert manifest["pages"][0]["headers"] == {}
    prefix = "CHG" if entity == "change_request" else "PRB"
    assert [r["number"]["value"][:3] for r in body["result"]] == [prefix] * 7


def test_ut11_67_page_size_and_determinism(tmp_path: Path) -> None:
    """UT11-67 `page_size` sets the page length; the same seed writes the same bytes."""
    first, second = tmp_path / "a", tmp_path / "b"
    assert write_api_pages(9, "servicenow", "incident", 30, first, page_size=7) == 5
    assert write_api_pages(9, "servicenow", "incident", 30, second, page_size=7) == 5
    for path in sorted(first.iterdir()):
        assert path.read_bytes() == (second / path.name).read_bytes()
    assert sorted(p.name for p in first.iterdir()) == [
        "manifest.json",
        *(f"page-0000{n}.json" for n in range(1, 6)),
    ]


@pytest.mark.parametrize(
    ("source", "entity", "rows", "page_size"),
    [
        ("servicenow", "incident", 0, 1000),
        ("servicenow", "incident", 5_000_001, 1000),
        ("servicenow", "incident", 10, 0),
        ("servicenow", "incident", 10, 2001),
        ("servicenow", "issue", 10, 1000),
        ("jira", "incident", 10, 1000),
        ("dataverse", "incident", 10, 1000),
    ],
)
def test_ut11_67_invalid_arguments_raise(
    tmp_path: Path, source: Any, entity: str, rows: int, page_size: int
) -> None:
    """UT11-67 invalid arguments raise `SynthUsageError` and write nothing."""
    out = tmp_path / "pages"
    with pytest.raises(SynthUsageError):
        write_api_pages(3, source, entity, rows, out, page_size=page_size)
    assert not out.exists()


def test_ut11_67_negative_seed_and_non_empty_out_raise(tmp_path: Path) -> None:
    """UT11-67 a negative seed or a non-empty `out` raises `SynthUsageError`; the existing
    file is left alone."""
    with pytest.raises(SynthUsageError):
        write_api_pages(-1, "jira", "issue", 5, tmp_path / "neg")
    busy = tmp_path / "busy"
    busy.mkdir()
    (busy / "keep.txt").write_text("x\n", encoding="utf-8")
    with pytest.raises(SynthUsageError):
        write_api_pages(3, "jira", "issue", 5, busy)
    assert [p.name for p in busy.iterdir()] == ["keep.txt"]
