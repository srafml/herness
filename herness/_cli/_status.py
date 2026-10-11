"""``herness status`` reads (impl 09 U09-93 row ``status``) and the CURRENT build reader the
``doctor`` row ``current_build`` shares (U09-94); private sibling of ``cmd_system`` (budget).

Every owner module is imported inside a function so ``herness --help`` stays fast (BT09-07).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Final

from herness.core.errors import ConfigError

if TYPE_CHECKING:
    from datetime import datetime

    from herness.core.config import HernessConfig

__all__ = ["NO_CURRENT", "BuildView", "collect_status", "current_build", "dq_failures"]

NO_CURRENT: Final = "No promoted warehouse yet."
_FINISHED_SQL: Final = "SELECT epoch_us(finished_at) FROM meta.build LIMIT 1"
_DQ_SQL: Final = (
    "SELECT check_name, severity, value, threshold FROM meta.dq_result"
    " WHERE NOT passed ORDER BY check_name LIMIT 50"
)
_US_PER_H: Final = 3_600_000_000
_JOB_FIELDS: Final = {"job_id", "status", "created_at", "started_at", "finished_at"}
_SNAPSHOT_KEYS: Final = ("workers", "running", "queue", "planned_rekey", "breakers",
                         "faults_enabled")  # fmt: skip


@dataclass(frozen=True)
class BuildView:
    """The CURRENT build id and its age in hours (None without a ``finished_at``)."""

    build_id: str
    age_h: float | None


def current_build(now: datetime) -> BuildView | None:
    """The promoted build from ``meta.build`` over a read-only connection; None without
    CURRENT. Store errors propagate (the callers wrap them)."""
    from herness.store import warehouse  # noqa: PLC0415 - lazy (BT09-07)

    build_id = warehouse.read_current()
    if build_id is None:
        return None
    with warehouse.open_readonly(build_id) as con:
        row = con.execute(_FINISHED_SQL).fetchone()
    finished = row[0] if row is not None else None
    if not isinstance(finished, int):
        return BuildView(build_id, None)
    now_us = int(now.timestamp() * 1_000_000)
    return BuildView(build_id, round((now_us - finished) / _US_PER_H, 1))


def dq_failures(build_id: str) -> list[dict[str, object]]:
    """Failed ``meta.dq_result`` rows of ``build_id`` (at most 50, by check name)."""
    from herness.store import warehouse  # noqa: PLC0415 - lazy (BT09-07)

    with warehouse.open_readonly(build_id) as con:
        rows = con.execute(_DQ_SQL).fetchall()
    keys = ("check_name", "severity", "value", "threshold")
    return [dict(zip(keys, row, strict=True)) for row in rows]


def _bind_chains(cfg: HernessConfig) -> str | None:
    """Bind the chain registry ``chat_policy`` reads (R-04), as ``cmd_chat`` does; a profile
    whose model clients cannot be built leaves the chat and jobs sections empty."""
    from herness.core.resilience import bind_chain_registry  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness.llm.registry import LLMRegistry  # noqa: PLC0415

    try:
        bind_chain_registry(LLMRegistry(cfg.models, profile=cfg.profile))
    except ConfigError as exc:
        return f"chat mode and job status unavailable ({type(exc).__name__})"
    return None


def collect_status(cfg: HernessConfig, now: datetime) -> tuple[dict[str, object], list[str]]:
    """The U09-93 ``status`` sections and warnings; a missing CURRENT leaves
    ``current_build`` None."""
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)
    from herness.store.ops import ui_reads  # noqa: PLC0415

    build = current_build(now)
    last = jobs.list_jobs(kind="build_pipeline", limit=1)
    unbound = _bind_chains(cfg)
    snapshot = None if unbound else jobs.status_snapshot(now)
    data: dict[str, object] = {
        "current_build": None if build is None else {"build_id": build.build_id,
                                                     "age_h": build.age_h},
        "last_build_job": last[0].model_dump(mode="json", include=_JOB_FIELDS) if last else None,
        "dq_warnings": [] if build is None else dq_failures(build.build_id),
        "jobs": None if snapshot is None else {k: snapshot[k] for k in _SNAPSHOT_KEYS},
        "runs": ui_reads.ui_list_runs(limit=5),
        "chat_mode": None if unbound else jobs.chat_policy(now),
    }  # fmt: skip
    return data, [] if unbound is None else [unbound]
