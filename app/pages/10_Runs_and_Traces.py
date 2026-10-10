"""Runs & Traces page (impl 09 U09-82; design §5.4): runs, one run's detail, jobs, trace, actions.

Task errors are shown as stored (spec 08 redacts them); job errors show the error class only.
Trace events come from ``read_trace_page``, which refuses a bad ``run_id`` or path (TH09-18),
bounds lines and pages, and keeps payloads and tool args only for roles with
``view_trace_payload`` (admin); they are rendered with ``st.json``, never as Markdown. Resume,
retry, cancel and re-render controls are rendered only for roles that pass the actions' own
checks (admin; TH09-07) and act only through ``herness.reports.actions`` (audited, TH09-19).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Final, Literal, cast

import streamlit as st

from app.common import data
from app.common._pages_08_10 import Row, act, allowed, cell, choose, flash, rows
from app.common.data import read_trace_page
from app.common.sanitize import safe_markdown
from app.common.widgets import PageContext, block, page
from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.types import ReportManifest
from herness.reports.actions import job_control, rerender_report, resume_run

# Trace event types of spec 05 §5.7.
TRACE_TYPES: Final = (
    "llm_call", "tool_call", "retry", "repair", "fallback", "guard_stop", "budget", "compaction",
    "verifier_verdict", "spawn_decision",
)  # fmt: skip
ACTIVE_JOBS: Final = ("queued", "running")
_JOB_OPS: Final[tuple[tuple[Literal["cancel", "retry"], str, tuple[str, ...]], ...]] = (
    ("retry", "job_retry", ("failed",)), ("cancel", "job_cancel", ACTIVE_JOBS),
)  # fmt: skip
NO_RUNS: Final = "No runs yet."


def _tokens(usage: object) -> int | None:
    """``input + output`` of a run's ``token_usage``."""
    if not isinstance(usage, Mapping):
        return None
    return sum(v for v in (usage.get("input"), usage.get("output")) if isinstance(v, int))


def _duration(run: Row) -> str:
    if not run["finished_at"]:
        return "—"
    try:
        took = clock.parse_iso(str(run["finished_at"])) - clock.parse_iso(str(run["started_at"]))
    except SchemaViolation:
        return "—"
    return f"{int(took.total_seconds())} s"


def _runs() -> str | None:
    """The run table (newest first) and the run selection; None when there is no run."""
    found = rows("ui_list_runs", limit=100)
    if not found:
        st.info(NO_RUNS)
        return None
    st.dataframe(
        [{"run_id": cell(r["run_id"]), "kind": cell(r["kind"]), "depth": cell(r["depth"]),
          "status": cell(r["status"]), "duration": _duration(r),
          "tokens": _tokens(r["token_usage"]), "cost_usd": cell(r["cost_usd"])}
         for r in found],
        hide_index=True,
    )  # fmt: skip
    ids = [str(r["run_id"]) for r in found]
    chosen: str | None = st.selectbox("Run", ids, key="runs_run")
    return chosen or ids[0]


def _table(found: list[dict[str, object]], empty: str) -> None:
    if found:
        st.dataframe(found, hide_index=True)
    else:
        st.caption(empty)


def _detail(run_id: str) -> None:
    tasks = rows("ui_list_tasks", run_id=run_id)
    if dead := [str(t["task_id"]) for t in tasks if t["status"] == "dead"]:
        st.warning(safe_markdown("Dead tasks: " + ", ".join(dead)))
    _table([{"task_id": cell(t["task_id"]), "role": cell(t["role"]), "status": cell(t["status"]),
             "attempts": t["attempts"], "objective": cell(t["objective"]),
             "last_error": cell(t["last_error"])} for t in tasks], "No tasks.")  # fmt: skip
    counts = cast("Mapping[str, int]", data.ops_read("ui_finding_status_counts", run_id=run_id))
    _table([{"finding_status": s, "findings": n} for s, n in sorted(counts.items())],
           "No findings.")  # fmt: skip
    events = rows("ui_list_resilience_events", run_id=run_id)
    _table([{"ts": cell(e["ts"]), "kind": cell(e["kind"]), "component": cell(e["component"]),
             "target": cell(e["target"]), "job_id": cell(e["job_id"]),
             "task_id": cell(e["task_id"])} for e in events],
           "No resilience events.")  # fmt: skip


def _error_class(raw: object) -> str:
    return cell(raw.get("class")) if isinstance(raw, Mapping) else "—"


def _control(ctx: PageContext, job_id: str, op: Literal["cancel", "retry"]) -> None:
    act(op.capitalize(), lambda: job_control(ctx.actor, job_id=job_id, op=op),
        lambda result: f"Job {job_id}: {result}.")  # fmt: skip


def _job_buttons(ctx: PageContext, job: Row) -> None:
    """Retry for a failed job, Cancel for a queued or running one (admin)."""
    job_id, status = str(job["job_id"]), str(job["status"])
    for op, action, states in _JOB_OPS:
        shown = status in states and allowed(ctx.actor, action)
        if shown and st.button(f"{op.capitalize()} job {job_id}", key=f"{action}_{job_id}"):
            _control(ctx, job_id, op)


def _jobs(ctx: PageContext, run_id: str) -> None:
    found = rows("ui_jobs_for_run", run_id=run_id)
    _table([{"job_id": cell(j["job_id"]), "kind": cell(j["kind"]), "status": cell(j["status"]),
             "attempts": f"{j['attempts']}/{j['max_attempts']}",
             "error": _error_class(j["last_error"]), "created_at": cell(j["created_at"])}
            for j in found], "No jobs for this run.")  # fmt: skip
    for job in found:
        _job_buttons(ctx, job)


def _actions(ctx: PageContext, run_id: str) -> None:
    """Resume (with ``retry_dead`` / ``force``) and re-render, each shown only to its role."""
    if allowed(ctx.actor, "run_resume"):
        with st.form("resume_form"):
            retry_dead = st.checkbox("Retry dead tasks", key="resume_retry_dead")
            force = st.checkbox("Force (resume a finished run)", key="resume_force")
            sent = st.form_submit_button("Resume run", key="resume_submit")
        if sent:
            act("Resume",
                lambda: resume_run(ctx.actor, run_id=run_id, retry_dead=retry_dead, force=force),
                lambda job_id: f"Resume queued as job {job_id}.")  # fmt: skip
    if not allowed(ctx.actor, "report_rerender"):
        return
    if st.button("Re-render report", key="report_rerender"):
        act("Re-render", lambda: rerender_report(ctx.actor, run_id=run_id), _rendered)


def _rendered(manifest: ReportManifest) -> str:
    return f"Report re-rendered: {', '.join(sorted(manifest.files))}."


def _trace(ctx: PageContext, run_id: str) -> None:
    left, right = st.columns(2)
    with left:
        kind = choose("Event type", TRACE_TYPES, key="trace_type")
    with right:
        number = st.number_input("Page", min_value=0, value=0, step=1, key="trace_page")
    found = read_trace_page(run_id, type_filter=kind, page=int(number),
                            include_payload=allowed(ctx.actor, "view_trace_payload"))  # fmt: skip
    if found.note is not None:
        st.caption(found.note)
    more = " · more on the next page" if found.has_next else ""
    skipped = f" · {found.skipped_lines} unreadable lines skipped" if found.skipped_lines else ""
    st.caption(f"Page {found.page} · {len(found.events)} events{more}{skipped}")
    if found.events:
        st.json(found.events, expanded=2)


def body(ctx: PageContext) -> None:
    """Runs, then the chosen run's detail, jobs, admin actions and trace, each block isolated."""
    st.title("Runs & Traces")
    flash()
    st.subheader("Runs")
    run_id: str | None = None
    with block("Runs"):
        run_id = _runs()
    if run_id is None:
        return
    st.subheader("Run detail")
    with block("Run detail"):
        _detail(run_id)
    st.subheader("Jobs")
    with block("Jobs"):
        _jobs(ctx, run_id)
    if allowed(ctx.actor, "run_resume") or allowed(ctx.actor, "report_rerender"):
        st.subheader("Actions")
        with block("Actions"):
            _actions(ctx, run_id)
    st.subheader("Trace viewer")
    with block("Trace viewer"):
        _trace(ctx, run_id)


page("Runs & Traces", body, needs_warehouse=False)
