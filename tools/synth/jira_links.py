"""INC/CHG mentions of Jira issues (U11-08 step 6).

Split out of `tools.synth.jira` for the module line budget. A mention names a ticket of
the 30 days before the issue's `created`: an `INC` number of a same-service incident from
the U11-19 `IncidentTimeIndex` when one is given, else (and always for `CHG`, which has no
index) a number from the catalog's planned sequence range, interpolated by time within
each month (`Catalog.seq_start` and `month_counts`), so the number exists in the lake.
"""

import math
from datetime import UTC, date, datetime, time, timedelta
from typing import Any, Final

import numpy as np

from tools.synth.catalog_rows import Catalog
from tools.synth.shards import IncidentTimeIndex

WINDOW: Final = timedelta(days=30)
_TABLES: Final = {"INC": "incident", "CHG": "change_request"}
_LINK_HOST: Final = "https://servicenow.example.com"
_TICKET_SHARE_INC: Final = 0.5


def _month_bounds(at: datetime) -> tuple[date, float]:
    """First day of `at`'s month and the elapsed fraction of that month."""
    first = at.date().replace(day=1)
    start = datetime.combine(first, time(), UTC)
    nxt = datetime.combine((first.replace(day=28) + timedelta(days=4)).replace(day=1), time(), UTC)
    return first, (at - start) / (nxt - start)


def _seq_at(cat: Catalog, entity: str, at: datetime) -> int | None:
    """Planned sequence number reached at `at`, or None outside the planned months."""
    month, fraction = _month_bounds(at)
    key = ("servicenow", entity, month)
    if key not in cat.seq_start:
        return None
    return cat.seq_start[key] + math.floor(cat.month_counts.get(key, 0) * fraction)


def _planned(cat: Catalog, rng: np.random.Generator, prefix: str, created: datetime) -> str | None:
    entity = _TABLES[prefix]
    high = _seq_at(cat, entity, created)
    if high is None:
        return None
    starts = [
        s for (src, ent, _), s in cat.seq_start.items() if (src, ent) == ("servicenow", entity)
    ]
    low = _seq_at(cat, entity, created - WINDOW) or min(starts)
    if high <= low:
        return None
    return f"{prefix}{int(rng.integers(low, high)):07d}"


def _indexed(
    index: IncidentTimeIndex, rng: np.random.Generator, service_sys_id: str, created: datetime
) -> str | None:
    opened = index.opened_at.get(service_sys_id)
    if opened is None:
        return None
    t1 = int(created.timestamp())
    lo, hi = np.searchsorted(opened, [t1 - int(WINDOW.total_seconds()), t1], side="left")
    if hi <= lo:
        return None
    return index.numbers[service_sys_id][int(rng.integers(lo, hi))]


def pick_ticket(
    cat: Catalog,
    rng: np.random.Generator,
    service_sys_id: str,
    created: datetime,
    index: IncidentTimeIndex | None,
) -> str | None:
    """An `INC` or `CHG` number (even odds) of the 30 days before `created`, else None."""
    if rng.random() < _TICKET_SHARE_INC:
        if index is not None:
            return _indexed(index, rng, service_sys_id, created)
        return _planned(cat, rng, "INC", created)
    return _planned(cat, rng, "CHG", created)


def remote_link(link_id: str, ticket: str) -> dict[str, Any]:
    """Jira remote link `{id, object: {url, title}}` to the ServiceNow record `ticket`."""
    table = _TABLES[ticket[:3]]
    url = f"{_LINK_HOST}/nav_to.do?uri={table}.do?sysparm_query=number={ticket}"
    return {"id": int(link_id), "object": {"url": url, "title": ticket}}


__all__ = ["WINDOW", "pick_ticket", "remote_link"]
