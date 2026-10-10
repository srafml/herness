"""Cached ops reads, cache invalidation and trace pages for the dashboard (impl 09 U09-64).

``ops_read`` serves only allowlisted read functions (the U09-52 ``ui_*`` reads, impl 02's
review-item reads and ``status_snapshot``), cached on ``(fn_name, params)`` and never on user
identity (TH09-29). Chat reads (U09-49) are never routed here. ``read_trace_page`` refuses a
bad ``run_id``, a symlink or a path outside ``<paths.data>/traces`` before reading (TH09-18).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, replace
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any, Final, cast

import streamlit as st

from herness.core.errors import ConfigError
from herness.core.jobs import status_snapshot
from herness.reports.contract import RUN_ID_RE
from herness.reports.rules import UserInputError
from herness.store.ops import ui_reads
from herness.store.ops.shared import ReviewItem, get_review_item, list_review_items

if TYPE_CHECKING:
    from app.common.bootstrap import AppServices

__all__ = ["OPS_READS", "OPS_READ_NAMES", "TracePage", "after_write", "ops_read", "read_trace_page"]

# The U09-52 read functions, named explicitly: a new ui_* helper is never allowlisted implicitly.
OPS_READ_NAMES: Final = (
    "ui_evidence_ids_present", "ui_get_evidence_rows", "ui_run_rec_ids", "ui_get_recommendation",
    "ui_finding_ids_with_status", "ui_finding_query_ids", "ui_finding_status_counts",
    "ui_task_status_counts", "ui_list_tasks", "ui_list_runs", "ui_list_recommendations",
    "ui_list_findings_for_entity", "ui_list_run_findings", "ui_jobs_for_run",
    "ui_latest_decisions", "ui_list_outcomes", "ui_list_memory_items",
    "ui_list_resilience_events", "ui_list_source_health", "ui_job_exists",
    "ui_job_status_counts", "ui_oldest_queued_age_s", "ui_failed_jobs_since",
    "list_review_items", "get_review_item", "status_snapshot",
)  # fmt: skip
OPS_READS: Final[Mapping[str, Callable[..., object]]] = {
    **{n: getattr(ui_reads, n) for n in OPS_READ_NAMES if n.startswith("ui_")},
    "list_review_items": list_review_items,
    "get_review_item": get_review_item,
    "status_snapshot": status_snapshot,
}
PAGE_SIZE: Final = 200
MAX_LINE_BYTES: Final = 65_536
MAX_SCAN_BYTES: int = 50 * 1024 * 1024  # module attribute so tests can shrink it
_DROPPED: Final = ("payload", "args")
_CHUNK: Final = 1 << 16


@dataclass(frozen=True)
class TracePage:
    events: list[dict[str, object]]
    page: int
    has_next: bool
    skipped_lines: int
    note: str | None


def _services() -> AppServices:
    from app.common.bootstrap import get_services  # noqa: PLC0415 - cycle: bootstrap imports wh

    return get_services()


def _picklable(value: object) -> object:
    """``ReviewItem`` payloads as dicts: impl 02's read-only mapping cannot be pickled by
    ``st.cache_data`` (T09-18); every cache hit unpickles a fresh copy, so nothing is shared."""
    if isinstance(value, ReviewItem):
        return replace(value, payload=dict(value.payload))
    if isinstance(value, list):
        return [_picklable(item) for item in value]
    return value


def _call(fn_name: str, params: dict[str, Any]) -> object:
    """The cached body: key ``(fn_name, params)`` only."""
    return _picklable(OPS_READS[fn_name](**params))


@functools.cache
def _cached(ttl: int) -> Callable[[str, dict[str, Any]], object]:
    """``st.cache_data`` over ``_call`` with the configured TTL (one wrapper per process)."""
    call: Callable[[str, dict[str, Any]], object]
    call = st.cache_data(ttl=ttl, max_entries=500, show_spinner=False)(_call)
    return call


def _ops() -> Callable[[str, dict[str, Any]], object]:
    return _cached(_services().cfg.app.app.cache_ttl_s.ops)


def ops_read(fn_name: str, /, **params: object) -> object:
    """Call an allowlisted ops read through the ops cache; unknown name -> ``ConfigError``."""
    if fn_name not in OPS_READS:
        msg = f"{fn_name[:64]} is not an allowlisted ops read"
        raise ConfigError(msg)
    return _ops()(fn_name, params)


def after_write() -> None:
    """Clear the ops cache after this session wrote (design §5.3)."""
    cast("Any", _ops()).clear()


def _trace_path(run_id: object) -> Path | None:
    """The contained trace file, ``None`` when missing; refusals raise ``UserInputError``."""
    if not isinstance(run_id, str) or RUN_ID_RE.fullmatch(run_id) is None:
        msg = "Invalid run id."
        raise UserInputError(msg)
    traces = Path(_services().cfg.paths.data) / "traces"
    path = traces / f"{run_id}.jsonl"
    if path.is_symlink() or path.resolve().parent != traces.resolve():
        msg = "Trace path refused."
        raise UserInputError(msg)
    return path if path.is_file() else None


def _lines(fh: IO[bytes], stopped: list[bool]) -> Iterator[bytes | None]:
    """Raw lines, ``None`` for an over-long one; sets ``stopped`` at the scan cap."""
    scanned = 0
    while raw := fh.readline(MAX_LINE_BYTES + 1):
        scanned += len(raw)
        line: bytes | None = raw
        if len(raw) > MAX_LINE_BYTES and not raw.endswith(b"\n"):
            line = None  # drain the rest of the line; drained bytes count toward the cap
            while scanned < MAX_SCAN_BYTES and (rest := fh.readline(_CHUNK)):
                scanned += len(rest)
                if rest.endswith(b"\n"):
                    break
        yield line
        if scanned >= MAX_SCAN_BYTES:
            stopped.append(True)
            return


def _parse(line: bytes) -> dict[str, object] | None:
    try:
        value = json.loads(line.decode("utf-8", errors="replace"))
    except ValueError:
        return None
    return value if isinstance(value, dict) else None


def read_trace_page(
    run_id: str, *, type_filter: str | None, page: int, include_payload: bool
) -> TracePage:
    """One page of ≤ 200 trace events of ``run_id`` (U09-64)."""
    if type(page) is not int or page < 0:
        msg = "Invalid page."
        raise UserInputError(msg)
    path = _trace_path(run_id)
    if path is None:
        return TracePage([], page, False, 0, "No trace file for this run.")
    first, stopped = page * PAGE_SIZE, list[bool]()
    skipped, seen, events = 0, 0, list[dict[str, object]]()
    with path.open("rb") as fh:
        for line in _lines(fh, stopped):
            event = None if line is None else _parse(line)
            if event is None:
                skipped += 1 if line is None or line.strip() else 0
            elif type_filter is None or event.get("type") == type_filter:
                seen += 1
                if first < seen <= first + PAGE_SIZE:
                    events.append(event)
    if not include_payload:
        events = [{k: v for k, v in e.items() if k not in _DROPPED} for e in events]
    note = "Trace truncated at 50 MB." if stopped else None
    return TracePage(events, page, seen > first + PAGE_SIZE, skipped, note)
