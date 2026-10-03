"""Row views of `status_snapshot` (private sibling of `status`, T08-22).

The nested `TypedDict`s of the U08-91 keys (§3.17 table). Kept apart from
`herness.core.jobs.status` for the impl 08 §2 line budget of that module.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, TypedDict

from herness.core.types import BreakerState, GpuClass, JobKind

if TYPE_CHECKING:
    from herness.core.jobs.ports import NextJob
    from herness.core.jobs.windows import ActiveWindow

__all__ = [
    "BreakerView",
    "NextJobView",
    "QueueView",
    "RekeyView",
    "RunningView",
    "ScheduleView",
    "WindowView",
    "WorkerView",
    "next_job_view",
    "window_view",
]


class WorkerView(TypedDict):
    worker_id: str
    host: str
    pid: int
    status: str
    heartbeat_age_s: float
    gpu_slot: int
    cpu_slots: int
    gpu_class_loaded: str
    requested_class: GpuClass | None
    services: dict[str, bool]
    faults_enabled: bool


class WindowView(TypedDict):
    name: str
    start_at: datetime
    end_at: datetime
    classes: list[str]
    preload: str | None


class RunningView(TypedDict):
    job_id: str
    kind: JobKind
    attempt: int
    max_attempts: int
    lease_left_s: float | None
    slot: str | None
    heartbeat_note: str | None


class RekeyView(TypedDict):
    job_id: str
    scheduled_for: datetime | None


class NextJobView(TypedDict):
    job_id: str
    kind: JobKind
    priority: int
    scheduled_for: datetime


class QueueView(TypedDict):
    queued: int
    next_job: NextJobView | None


class BreakerView(TypedDict):
    key: str
    state: BreakerState
    since: datetime | None
    trips: int
    probe_due: datetime | None


class ScheduleView(TypedDict):
    name: str
    next_fire_at: datetime


def window_view(active: ActiveWindow) -> WindowView:
    """The U08-66 window as a view."""
    spec = active.spec
    return WindowView(
        name=spec.name,
        start_at=active.start_at,
        end_at=active.end_at,
        classes=list(spec.classes),
        preload=spec.preload,
    )


def next_job_view(head: NextJob) -> NextJobView:
    """The `queue_stats` head job as a view."""
    return NextJobView(
        job_id=head.job_id, kind=head.kind, priority=head.priority, scheduled_for=head.scheduled_for
    )
