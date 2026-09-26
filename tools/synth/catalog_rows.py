"""Frozen row types of the synthetic catalog (U11-04).

Split out of `tools.synth.catalog` for the module line budget; `tools.synth.catalog`
re-exports every name. The catalog is pickled once per worker pool, so every field is a
plain value, tuple or dict.
"""

import dataclasses
from collections.abc import Callable, Mapping
from datetime import date, datetime

from tools.synth.params import SynthParams

MonthKey = tuple[str, str, date]  # (source, entity, first day of month)


@dataclasses.dataclass(frozen=True, slots=True)
class OrgRow:
    sys_id: str
    name: str
    cost_center: str


@dataclasses.dataclass(frozen=True, slots=True)
class TeamRow:
    sys_id: str
    name: str
    org_sys_id: str
    manager_name: str
    cost_center: str
    mttr_multiplier: float
    reassign_extra: float


@dataclasses.dataclass(frozen=True, slots=True)
class ServiceRow:
    sys_id: str
    name: str
    criticality: int
    owner_team_sys_id: str
    support_team_sys_id: str
    incident_weight: float
    event_weight: float
    jira_project: str
    jira_component: str
    cost_center: str
    annual_run_cost_usd: float
    downtime_cost_per_hour_usd: float


@dataclasses.dataclass(frozen=True, slots=True)
class CiRow:
    sys_id: str
    name: str
    sys_class_name: str
    service_sys_id: str


@dataclasses.dataclass(frozen=True, slots=True)
class RelRow:
    sys_id: str
    parent: str
    child: str
    type: str


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectRow:
    key: str
    name: str
    id_base: int


@dataclasses.dataclass(frozen=True, slots=True)
class ChangeSlot:
    """One T3 change on C3; `seq` is its 0-based position in `work_end` order.

    `caused_by` holds one flag per follow-up incident (in opening-draw order): true when
    that incident's `caused_by` names the change. The flags are drawn once at catalog
    build from the `plants:t3` stream, so every shard agrees (U11-12)."""

    sys_id: str
    seq: int
    work_end: datetime
    emergency: bool
    follow_up_incidents: int
    caused_by: tuple[bool, ...] = ()


@dataclasses.dataclass(frozen=True, slots=True)
class PlantTargets:
    """Service, team and CI targets of T1-T6 (sys_ids). Epics: E2 on `s2`, decoy E2d on
    `e2d_service`, E6p on `s6p`, E6u on `s6u`. `peak_window` is an inclusive `MM-DD` pair
    that wraps the year end when its start sorts after its end."""

    t1_team: str
    t1_services: tuple[str, ...]
    s2: str
    s2c: str
    e2d_service: str
    s3: str
    c3: str
    s4: str
    t5_team: str
    s5: str
    s6p: str
    s6u: str
    s6_peers: tuple[str, ...]
    effective_at: datetime
    peak_window: tuple[str, str]

    @property
    def services(self) -> tuple[str, ...]:
        rest = (self.s2, self.s2c, self.e2d_service, self.s3, self.s4, self.s5)
        return (*self.t1_services, *rest, self.s6p, self.s6u)

    @property
    def teams(self) -> tuple[str, str]:
        return (self.t1_team, self.t5_team)

    def in_peak(self, day: date) -> bool:
        low, high = self.peak_window
        md = day.strftime("%m-%d")
        return low <= md <= high if low <= high else (md >= low or md <= high)


@dataclasses.dataclass(frozen=True, slots=True)
class Catalog:
    orgs: tuple[OrgRow, ...]
    teams: tuple[TeamRow, ...]
    services: tuple[ServiceRow, ...]
    cis: tuple[CiRow, ...]
    rels: tuple[RelRow, ...]
    projects: tuple[ProjectRow, ...]
    change_schedule: tuple[ChangeSlot, ...]
    plants: PlantTargets
    month_counts: dict[MonthKey, int]
    seq_start: dict[MonthKey, int]
    # (planner id, month key) -> (first seq, count) of that planner's plant records; plant
    # records follow the month's background records in planner order (see catalog_plan).
    plant_seq: dict[tuple[str, MonthKey], tuple[int, int]] = dataclasses.field(default_factory=dict)


Planner = Callable[[Catalog, SynthParams], Mapping[MonthKey, int]]
