"""Fact row and frame types of `tests.support.metrics_oracle` (split for the ENG 400-line limit)."""

import dataclasses
import datetime
from collections.abc import Mapping, Sequence
from zoneinfo import ZoneInfo

__all__ = ["Change", "Facts", "Frame", "Incident", "WorkItem"]


@dataclasses.dataclass(frozen=True)
class Incident:
    """The `metrics.incident_fact` columns the covered metrics read."""

    record_id: str
    opened_at: datetime.datetime
    resolved_at: datetime.datetime | None
    priority: int | None
    service_id: str | None
    team_id: str | None
    org_id: str | None
    cluster_id: str | None
    excluded: bool
    resolve_h: float | None
    is_repeat: bool
    is_reopened: bool
    is_reassigned: bool
    sla_breached: bool | None
    toil_h: float


@dataclasses.dataclass(frozen=True)
class Change:
    """The `metrics.change_fact` columns the covered metrics read."""

    record_id: str
    type: str
    service_id: str | None
    team_id: str | None
    org_id: str | None
    actual_end: datetime.datetime | None
    deployed: bool
    failed: bool
    lead_time_h: float | None


@dataclasses.dataclass(frozen=True)
class WorkItem:
    """A `metrics.work_item_fact` row plus its parent and `core.work_item_transition` rows."""

    record_id: str
    type: str
    parent_id: str | None
    service_id: str | None
    team_id: str | None
    org_id: str | None
    story_points: float | None
    created_at: datetime.datetime
    first_in_progress_at: datetime.datetime | None
    done_at: datetime.datetime | None
    cycle_days: float | None
    is_unplanned: bool
    transitions: tuple[tuple[datetime.datetime, str], ...]


@dataclasses.dataclass(frozen=True)
class Facts:
    """Fact rows and the org tree (org -> parent org)."""

    incidents: Sequence[Incident] = ()
    changes: Sequence[Change] = ()
    items: Sequence[WorkItem] = ()
    org_parents: Mapping[str, str | None] = dataclasses.field(default_factory=dict)


@dataclasses.dataclass(frozen=True)
class Frame:
    """Period and window of one compute call (the `Window` binds)."""

    period: str
    window_start: datetime.datetime
    window_end: datetime.datetime
    window_start_date: datetime.date
    window_end_date: datetime.date
    as_of: datetime.date
    tz: str

    @property
    def rolling(self) -> bool:
        return self.period in {"t12w", "t12m"}

    def local_midnight(self, day: datetime.date) -> datetime.datetime:
        return datetime.datetime.combine(day, datetime.time(0), tzinfo=ZoneInfo(self.tz))

    def contains(self, ts: datetime.datetime | None) -> bool:
        return ts is not None and self.window_start <= ts < self.window_end

    def trunc(self, day: datetime.date) -> datetime.date:
        if self.period == "week":
            return day - datetime.timedelta(days=day.weekday())
        if self.period == "month":
            return day.replace(day=1)
        return day.replace(month=3 * ((day.month - 1) // 3) + 1, day=1)

    def step(self, day: datetime.date) -> datetime.date:
        if self.period == "week":
            return day + datetime.timedelta(days=7)
        months = 1 if self.period == "month" else 3
        month = day.month - 1 + months
        return day.replace(year=day.year + month // 12, month=month % 12 + 1)

    def period_start(self, ts: datetime.datetime) -> datetime.date:
        if self.rolling:
            return self.window_start_date
        return self.trunc(ts.astimezone(ZoneInfo(self.tz)).date())

    def period_end(self, ps: datetime.date) -> datetime.date:
        return self.window_end_date if self.rolling else self.step(ps)

    def spine(self) -> list[tuple[datetime.date, datetime.date]]:
        """(period_start, period_end) rows of `period_spine()`."""
        if self.rolling:
            return [(self.window_start_date, self.window_end_date)]
        rows: list[tuple[datetime.date, datetime.date]] = []
        ps = self.trunc(self.window_start_date)
        while ps < self.window_end_date:
            rows.append((ps, self.step(ps)))
            ps = self.step(ps)
        return rows
