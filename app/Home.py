"""Overview page (impl 09 U09-72; design §5.4): builds, data quality, runs, jobs, workers, sources.

A viewer page with ``needs_warehouse=False``: the ops blocks render without a promoted build.
Every block is isolated by ``block`` (design §6). Only ids, statuses, counts, ages and the
class of a job's ``last_error`` are shown, never error messages or ticket text (TH09-11).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from typing import Final, cast

import streamlit as st

from app.common import data, wh
from app.common.sanitize import safe_markdown
from app.common.widgets import NO_BUILD, PageContext, block, page
from herness.core import time as clock
from herness.core.errors import SchemaViolation

DAY: Final = timedelta(hours=24)
NO_WORKER: Final = "No worker is running. Start one with `herness worker`."
RUNS_LINK: Final = "[Open Runs & Traces](/Runs_and_Traces)"
_UNITS: Final = (("d", 86_400), ("h", 3_600), ("min", 60))
type Row = Mapping[str, object]


def _age(seconds: float | None) -> str:
    """``2 h``, ``5 min``, ``40 s``; ``—`` when unknown."""
    if seconds is None:
        return "—"
    whole = max(0, int(seconds))
    return next((f"{whole // n} {u}" for u, n in _UNITS if whole >= n), f"{whole} s")


def _ts(value: object) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, str) and value:
        try:
            return clock.parse_iso(value)
        except SchemaViolation:
            return None
    return None


def _between(start: object, end: object) -> str:
    begin, finish = _ts(start), _ts(end)
    return "—" if begin is None or finish is None else _age((finish - begin).total_seconds())


def _text(value: object) -> str:
    return "—" if value is None else str(value)


def _rows(fn_name: str, /, **params: object) -> list[Row]:
    return list(cast("Sequence[Row]", data.ops_read(fn_name, **params)))


def _builds(ctx: PageContext) -> None:
    if ctx.build_id is None:
        st.info(NO_BUILD)
        return
    rows = wh.query("home.builds").table.to_pylist()
    st.dataframe(
        [
            {
                "build": _text(r["build_id"]),
                "current": "current" if r["build_id"] == ctx.build_id else "",
                "status": _text(r["status"]),
                "started": _text(r["started_at"]),
                "age": _between(r["finished_at"] or r["started_at"], ctx.now),
            }
            for r in rows
        ],
        hide_index=True,
    )


def _data_quality(ctx: PageContext) -> None:
    if ctx.build_id is None:
        st.caption("No promoted build")
        return
    rows = wh.query("home.dq_failed").table.to_pylist()
    if not rows:
        st.success("All checks passed")
        return
    columns = ("check_name", "severity", "value", "threshold")
    st.dataframe([{c: _text(r[c]) for c in columns} for r in rows], hide_index=True)


def _runs() -> None:
    rows = _rows("ui_list_runs", limit=10)
    if not rows:
        st.caption("No runs yet.")
    else:
        st.dataframe(
            [
                {
                    "run": _text(r["run_id"]),
                    "kind": _text(r["kind"]),
                    "depth": _text(r["depth"]),
                    "status": _text(r["status"]),
                    "duration": _between(r["started_at"], r["finished_at"]),
                    "cost_usd": _text(r["cost_usd"]),
                }
                for r in rows
            ],
            hide_index=True,
        )
    st.markdown(RUNS_LINK)


def _error_class(raw: object) -> str:
    value = raw.get("class") if isinstance(raw, Mapping) else None
    return _text(value)[:80]


def _jobs(ctx: PageContext) -> None:
    counts = cast("Mapping[str, int]", data.ops_read("ui_job_status_counts"))
    st.dataframe(
        [{"status": s, "jobs": str(n)} for s, n in sorted(counts.items())], hide_index=True
    )
    oldest = cast("float | None", data.ops_read("ui_oldest_queued_age_s", now=ctx.now))
    st.caption(f"Oldest queued job: {_age(oldest)}")
    snapshot = cast("Mapping[str, object]", data.ops_read("status_snapshot"))
    running = cast("Sequence[Row]", snapshot["running"])
    st.caption(f"Running: {len(running)}")
    if running:
        st.dataframe(
            [
                {"job": _text(j["job_id"]), "kind": _text(j["kind"]),
                 "attempt": f"{j['attempt']}/{j['max_attempts']}",
                 "lease_left": _age(cast("float | None", j["lease_left_s"]))}
                for j in running
            ],
            hide_index=True,
        )  # fmt: skip
    failed = _rows("ui_failed_jobs_since", since=ctx.now - DAY)
    st.caption(f"Failed in the last 24 h: {len(failed)}")
    if failed:
        st.dataframe(
            [
                {"job": _text(j["job_id"]), "kind": _text(j["kind"]),
                 "finished": _text(j["finished_at"]), "error": _error_class(j["last_error"])}
                for j in failed
            ],
            hide_index=True,
        )  # fmt: skip


def _workers() -> None:
    snapshot = cast("Mapping[str, object]", data.ops_read("status_snapshot"))
    workers = cast("Sequence[Row]", snapshot["workers"])
    if not workers:
        st.info(NO_WORKER)
        return
    st.dataframe(
        [
            {"worker": _text(w["worker_id"]), "status": _text(w["status"]),
             "gpu_class_loaded": _text(w["gpu_class_loaded"]),
             "heartbeat_age": _age(cast("float", w["heartbeat_age_s"]))}
            for w in workers
        ],
        hide_index=True,
    )  # fmt: skip


def _sources() -> None:
    rows = sorted(
        _rows("ui_list_source_health"), key=lambda r: (r["state"] == "closed", str(r["source"]))
    )
    if not rows:
        st.caption("No source health recorded yet.")
        return
    open_ = [str(r["source"]) for r in rows if r["state"] != "closed"]
    if open_:
        st.warning(f"Breaker not closed: {safe_markdown(', '.join(open_))}")
    columns = ("source", "state", "failures", "trips", "opened_at", "updated_at")
    st.dataframe(
        [{"attention": "⚠" if r["state"] != "closed" else "", **{c: _text(r[c]) for c in columns}}
         for r in rows],
        hide_index=True,
    )  # fmt: skip


def body(ctx: PageContext) -> None:
    """The Overview blocks, each isolated so one failure leaves the rest of the page."""
    st.title("Overview")
    sections: tuple[tuple[str, Callable[[], None]], ...] = (
        ("Builds", lambda: _builds(ctx)),
        ("Data quality", lambda: _data_quality(ctx)),
        ("Runs", _runs),
        ("Jobs", lambda: _jobs(ctx)),
        ("Workers", _workers),
        ("Sources", _sources),
    )
    for name, render in sections:
        st.subheader(name)
        with block(name):
            render()


page("Overview", body, needs_warehouse=False)
