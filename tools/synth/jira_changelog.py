"""Status lifecycle and changelog histories of Jira issues (U11-08 step 4).

Split out of `tools.synth.jira` for the module line budget. Status names map to categories
`To Do` -> `new`, `In Progress` -> `indeterminate`, `Done` -> `done`. Timestamps use the
Jira Cloud format `YYYY-MM-DDTHH:MM:SS.mmm+0000` in UTC.
"""

import calendar
import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import numpy as np

from tools.synth.param_groups import JiraParams

Step = tuple[datetime, str, str]  # (at, from status, to status)

TODO: Final = "To Do"
IN_PROGRESS: Final = "In Progress"
DONE: Final = "Done"
STATUS_IDS: Final = {TODO: "10000", IN_PROGRESS: "3", DONE: "10001"}
CATEGORIES: Final = {TODO: "new", IN_PROGRESS: "indeterminate", DONE: "done"}
_START_WAIT_H: Final = 72.0  # created -> first In Progress ~ U(0, 72 h); the spec leaves it open
_BOUNCE: Final = (0.2, 0.8)  # re-entry: back to To Do and In Progress again, as cycle fractions
_MIN_STEP: Final = timedelta(minutes=1)


def jira_ts(at: datetime) -> str:
    """Jira timestamp `YYYY-MM-DDTHH:MM:SS.mmm+0000` in UTC."""
    at = at.astimezone(UTC)
    return at.strftime("%Y-%m-%dT%H:%M:%S.") + f"{at.microsecond // 1000:03d}+0000"


def _month_after(day: date) -> tuple[datetime, int]:
    """Midnight UTC starting the month after `day`'s month, and that month's day count."""
    nxt = (day.replace(day=28) + timedelta(days=4)).replace(day=1)
    return datetime.combine(nxt, time(), UTC), calendar.monthrange(nxt.year, nxt.month)[1]


def transitions(
    jp: JiraParams, rng: np.random.Generator, created: datetime, end: datetime
) -> list[Step]:
    """To Do -> In Progress -> Done, the log-normal cycle time running from the first In
    Progress to Done (spec 04 `cycle_days`); with `reenter_in_progress_rate` the issue drops
    back to To Do and re-enters In Progress once; carried-over issues finish in the month
    after the creation month. Only transitions up to `end` are kept."""
    start = created + timedelta(hours=float(rng.uniform(0.0, _START_WAIT_H)))
    cycle = float(rng.lognormal(math.log(jp.cycle_time_median_days), jp.cycle_time_sigma))
    done = start + timedelta(days=cycle)
    if rng.random() < jp.carry_over_rate:
        nxt, days = _month_after(created.date())
        done = max(nxt + timedelta(days=float(rng.uniform(0.0, days))), start + _MIN_STEP)
    steps: list[Step] = [(start, TODO, IN_PROGRESS)]
    if rng.random() < jp.reenter_in_progress_rate:
        back, again = sorted(float(x) for x in rng.uniform(*_BOUNCE, 2))
        steps.append((start + (done - start) * back, IN_PROGRESS, TODO))
        steps.append((start + (done - start) * again, TODO, IN_PROGRESS))
    steps.append((done, IN_PROGRESS, DONE))
    return [s for s in steps if s[0] <= end]


def status(steps: list[Step]) -> tuple[str, datetime | None]:
    """Current status and `resolutiondate`: the last kept transition decides both."""
    current = steps[-1][2] if steps else TODO
    return current, steps[-1][0] if current == DONE else None


def changelog(issue_id: str, steps: list[Step]) -> dict[str, Any]:
    """The complete `changelog` object: one status history per transition."""
    histories = []
    for k, (at, old, new) in enumerate(steps):
        item = {"field": "status", "fieldtype": "jira", "fieldId": "status"}
        item |= {"from": STATUS_IDS[old], "fromString": old}
        item |= {"to": STATUS_IDS[new], "toString": new}
        histories.append({"id": f"{issue_id}{k:02d}", "created": jira_ts(at), "items": [item]})
    total = len(histories)
    return {"startAt": 0, "maxResults": total, "total": total, "histories": histories}


__all__ = [
    "CATEGORIES",
    "DONE",
    "IN_PROGRESS",
    "STATUS_IDS",
    "TODO",
    "Step",
    "changelog",
    "jira_ts",
    "status",
    "transitions",
]
