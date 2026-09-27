"""Per-source health for `herness doctor` and the dashboard (impl 01 U01-57, ENG §4).

The report reads breaker state (T08-06) and stored watermarks (U01-92) only: it makes no
source call and no network I/O. `herness doctor --sources` calls `Connector.check()`
separately (spec 09).
"""

from __future__ import annotations

import dataclasses
import datetime
from collections.abc import Iterator, Mapping
from typing import Final, Literal

from herness.connectors.settings import MonitoringSettings, SourcesConfig
from herness.core import time as clock
from herness.core.resilience import breaker
from herness.store.ops import Watermark, list_watermarks

__all__ = ["STALE_AFTER", "STALE_AFTER_MONITORING", "SourceHealth", "source_health_report"]

STALE_AFTER: Final = datetime.timedelta(hours=24)
STALE_AFTER_MONITORING: Final = datetime.timedelta(hours=48)
_HOUR_S: Final = 3600


@dataclasses.dataclass(frozen=True, slots=True)
class SourceHealth:
    """Health of one stream key; `reason` is empty when `status` is `ok`."""

    key: str
    status: Literal["ok", "degraded", "down"]
    reason: str


def _streams(cfg: SourcesConfig) -> Iterator[tuple[str, str, tuple[str, ...]]]:
    """(source, stream key, entities) of every enabled source; one key per monitoring tool."""
    for name, src in cfg.enabled_sources():
        entities = tuple(src.entities)
        if isinstance(src, MonitoringSettings):
            for tool, adapter in src.adapters.items():
                if adapter.enabled:
                    yield name, f"monitoring:{tool}", entities
        else:
            yield name, name, entities


def _watermark_reason(
    marks: Mapping[tuple[str, str], Watermark],
    key: str,
    entities: tuple[str, ...],
    *,
    limit: datetime.timedelta,
    now: datetime.datetime,
) -> str | None:
    """Why the stream's watermarks make it degraded, or None when they are all fresh."""
    missing = [entity for entity in entities if (key, entity) not in marks]
    if missing:
        return f"no watermark for {missing[0]}"
    for entity in entities:
        age = now - marks[(key, entity)].value
        if age > limit:
            return f"watermark stale for {entity}: {int(age.total_seconds() // _HOUR_S)} h"
    return None


def source_health_report(cfg: SourcesConfig, *, now: datetime.datetime) -> list[SourceHealth]:
    """One `SourceHealth` per stream key of each enabled source, in source order.

    An open breaker is `down`, a half-open one `degraded`; otherwise a source other than
    `files` is `degraded` while an entity has no watermark or one older than `STALE_AFTER`
    (`STALE_AFTER_MONITORING` for monitoring). Naive `now` raises SchemaViolation; the store
    reads raise StoreBusy.
    """
    now = clock.ensure_utc(now)
    marks = {(m.source, m.entity): m for m in list_watermarks()}
    report: list[SourceHealth] = []
    for source, key, entities in _streams(cfg):
        state = breaker(key).state()
        if state == "open":
            report.append(SourceHealth(key, "down", "breaker open"))
            continue
        if state == "half_open":
            report.append(SourceHealth(key, "degraded", "breaker probing"))
            continue
        reason = None
        if source != "files":
            limit = STALE_AFTER_MONITORING if source == "monitoring" else STALE_AFTER
            reason = _watermark_reason(marks, key, entities, limit=limit, now=now)
        report.append(SourceHealth(key, "ok", "") if reason is None else _degraded(key, reason))
    return report


def _degraded(key: str, reason: str) -> SourceHealth:
    return SourceHealth(key, "degraded", reason)
