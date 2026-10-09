"""UT09-78: cached ops reads, cache invalidation and trace pages (U09-64)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from tests.support.app_env import wh_services
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import add_run

from app.common import bootstrap, data
from app.common.data import TracePage
from herness.core.errors import ConfigError
from herness.core.jobs import status_snapshot
from herness.reports.rules import UserInputError
from herness.store.ops import ui_reads
from herness.store.ops.shared import get_review_item

pytestmark = pytest.mark.unit

RUN_ID = "run_" + "0" * 25 + "A"


@pytest.fixture(autouse=True)
def _fresh_caches() -> Iterator[None]:
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Services whose data root is ``tmp_path/data``; returns the traces directory."""
    services = wh_services(tmp_path / "data")
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    traces = tmp_path / "data" / "traces"
    traces.mkdir(parents=True)
    return traces


def _event(i: int, kind: str) -> str:
    return json.dumps({"type": kind, "seq": i, "payload": {"p": i}, "args": [i], "ok": True})


def _write_trace(traces: Path) -> None:
    lines: list[str] = []
    for i in range(450):
        lines.append(_event(i, "tool_call"))
        if i % 9 == 0:
            lines.append(_event(i, "llm_call"))  # 50 non-matching events
    lines.insert(10, '{"type": "tool_call", "pad": "' + "x" * 70_000 + '"}')  # too long
    lines.insert(20, "not json at all")
    lines.insert(30, "[1, 2, 3]")  # JSON but not an object
    lines.insert(40, "")  # blank lines are ignored, not counted
    (traces / f"{RUN_ID}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_ut09_78_paging_skips_and_payload_dropped(root: Path) -> None:
    """UT09-78 pages of 200 matches; long and non-object lines counted; payload/args dropped."""
    _write_trace(root)
    first = data.read_trace_page(RUN_ID, type_filter="tool_call", page=0, include_payload=False)
    assert isinstance(first, TracePage)
    assert len(first.events) == 200
    assert first.has_next is True
    assert first.page == 0
    assert first.skipped_lines == 3
    assert first.note is None
    assert [e["seq"] for e in first.events[:3]] == [0, 1, 2]
    assert all("payload" not in e and "args" not in e for e in first.events)
    last = data.read_trace_page(RUN_ID, type_filter="tool_call", page=2, include_payload=False)
    assert [len(last.events), last.has_next] == [50, False]
    assert last.events[-1]["seq"] == 449
    beyond = data.read_trace_page(RUN_ID, type_filter="tool_call", page=3, include_payload=False)
    assert beyond.events == []
    assert beyond.has_next is False


def test_ut09_78_exact_page_boundary(root: Path) -> None:
    """UT09-78 has_next only when a 201st match exists."""
    body = "\n".join(_event(i, "x") for i in range(200))
    (root / f"{RUN_ID}.jsonl").write_text(body, encoding="utf-8")
    page = data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=True)
    assert [len(page.events), page.has_next] == [200, False]


def test_ut09_78_admin_payload_and_no_filter(root: Path) -> None:
    """UT09-78 include_payload keeps `payload`/`args`; no filter keeps every event."""
    _write_trace(root)
    page = data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=True)
    assert len(page.events) == 200
    assert page.events[0]["payload"] == {"p": 0}
    assert {e["type"] for e in page.events} == {"tool_call", "llm_call"}


def test_ut09_78_missing_trace_file(root: Path) -> None:
    """UT09-78 no trace file -> empty page with a note."""
    page = data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=False)
    assert page == TracePage([], 0, False, 0, "No trace file for this run.")


def test_ut09_78_invalid_utf8_replaced(root: Path) -> None:
    """UT09-78 undecodable bytes are replaced, the line still parses."""
    raw = b'{"type": "t", "v": "\xff\xfe"}\n'
    (root / f"{RUN_ID}.jsonl").write_bytes(raw)
    page = data.read_trace_page(RUN_ID, type_filter="t", page=0, include_payload=False)
    assert page.events == [{"type": "t", "v": "��"}]


def test_ut09_78_scan_cap(root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-78 reading stops after the scan cap with a note."""
    _write_trace(root)
    monkeypatch.setattr(data, "MAX_SCAN_BYTES", 2_000)
    page = data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=False)
    assert page.note == "Trace truncated at 50 MB."
    assert 0 < len(page.events) < 200


def test_ut09_78_scan_cap_applies_inside_long_line(
    root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-78 bytes drained from an over-long line count toward the scan cap."""
    huge = b'{"type": "x", "pad": "' + b"y" * 4_000_000  # one 4 MB line, no newline yet
    (root / f"{RUN_ID}.jsonl").write_bytes(huge + b'"}\n' + _event(1, "x").encode() + b"\n")
    monkeypatch.setattr(data, "MAX_SCAN_BYTES", 200_000)
    consumed: list[int] = []
    real_open = Path.open

    class Counting:
        def __init__(self, fh: Any) -> None:
            self._fh = fh

        def readline(self, size: int = -1) -> bytes:
            chunk: bytes = self._fh.readline(size)
            consumed.append(len(chunk))
            return chunk

        def __enter__(self) -> Counting:
            return self

        def __exit__(self, *exc: object) -> None:
            self._fh.close()

    monkeypatch.setattr(Path, "open", lambda self, *a, **k: Counting(real_open(self, *a, **k)))
    page = data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=False)
    assert page.note == "Trace truncated at 50 MB."
    assert page.events == []
    assert sum(consumed) < 400_000  # stopped near the cap, not at the end of the 4 MB line


@pytest.mark.parametrize("bad", ["", "run_1", "../../x", RUN_ID.lower(), RUN_ID + "\n"])
def test_ut09_78_bad_run_id(root: Path, bad: str) -> None:
    """UT09-78 a run_id outside RUN_ID_RE -> UserInputError."""
    with pytest.raises(UserInputError):
        data.read_trace_page(bad, type_filter=None, page=0, include_payload=False)


@pytest.mark.parametrize("page", [-1, True])
def test_ut09_78_bad_page(root: Path, page: int) -> None:
    """UT09-78 a negative or boolean page -> UserInputError."""
    with pytest.raises(UserInputError):
        data.read_trace_page(RUN_ID, type_filter=None, page=page, include_payload=False)


# --- ops_read / after_write --------------------------------------------------------------------


@pytest.fixture
def ops_root(monkeypatch: pytest.MonkeyPatch, ops_store: OpsStoreHandle) -> OpsStoreHandle:
    services = wh_services(ops_store.data_root)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    return ops_store


def test_ut09_78_ops_read_cached_until_after_write(ops_root: OpsStoreHandle) -> None:
    """UT09-78 ops_read caches on (fn_name, params); after_write clears the ops cache."""
    assert data.ops_read("ui_list_runs", limit=5) == []
    run_id = add_run("done", None)
    assert data.ops_read("ui_list_runs", limit=5) == []  # cached
    assert [r["run_id"] for r in data.ops_read("ui_list_runs", limit=6)] == [run_id]  # new key
    data.after_write()
    assert [r["run_id"] for r in data.ops_read("ui_list_runs", limit=5)] == [run_id]


def test_ut09_78_ops_read_allowlist(ops_root: OpsStoreHandle) -> None:
    """UT09-78 impl 02 review reads and status_snapshot are allowlisted."""
    del ops_root
    assert data.ops_read("list_review_items") == []
    assert data.OPS_READS["status_snapshot"] is status_snapshot
    assert data.OPS_READS["get_review_item"] is get_review_item
    assert set(data.OPS_READS) == _ALLOWLIST


_ALLOWLIST = frozenset(
    {
        "ui_evidence_ids_present", "ui_get_evidence_rows", "ui_run_rec_ids",
        "ui_get_recommendation", "ui_finding_ids_with_status", "ui_finding_query_ids",
        "ui_finding_status_counts", "ui_task_status_counts", "ui_list_tasks", "ui_list_runs",
        "ui_list_recommendations", "ui_list_findings_for_entity", "ui_list_run_findings",
        "ui_jobs_for_run", "ui_latest_decisions", "ui_list_outcomes", "ui_list_memory_items",
        "ui_list_resilience_events", "ui_list_source_health", "ui_job_exists",
        "ui_job_status_counts", "ui_oldest_queued_age_s", "ui_failed_jobs_since",
        "list_review_items", "get_review_item", "status_snapshot",
    }
)  # fmt: skip


def test_ut09_78_allowlist_is_explicit() -> None:
    """UT09-78 the allowlist is an explicit name list bound to the real functions."""
    assert len(data.OPS_READ_NAMES) == len(_ALLOWLIST)
    assert set(data.OPS_READ_NAMES) == _ALLOWLIST
    for name in data.OPS_READ_NAMES:
        if name.startswith("ui_"):
            assert data.OPS_READS[name] is getattr(ui_reads, name)


@pytest.mark.parametrize(
    "name", ["list_chat_sessions", "list_chat_messages", "insert_run", "__import__", "ui_"]
)
def test_ut09_78_ops_read_unknown_name(ops_root: OpsStoreHandle, name: str) -> None:
    """UT09-78 a name outside the allowlist (chat reads included) -> ConfigError."""
    with pytest.raises(ConfigError, match="not an allowlisted ops read"):
        data.ops_read(name)
