"""Window coverage and cron checks: the `resilience` owner validator (U08-67, U08-99; R-03, R-71).

Settings modules may not import `herness.core.jobs.cron` (R-03), so the week coverage of
`schedule.windows` and the full parse of every cron 08 schedules run here. Composition roots
register `validate_resilience_config` with `register_owner_validator("resilience", ...)`
(R-71); this module never registers itself. Issues carry key paths and rule text only.
Source sections are read through `cfg.sources.enabled_sources()` (no connectors import).
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from typing import TYPE_CHECKING, Final
from zoneinfo import ZoneInfo

from herness.core import time as clock
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError
from herness.core.jobs.cron import CronExpr

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.resilience.settings import WindowSpec

__all__ = ["validate_resilience_config", "validate_windows"]

_DAYS: Final = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
_DAY_MIN: Final = 1440
_WEEK_MIN: Final = 7 * _DAY_MIN
_MAX_ISSUES: Final = 20
_DASH: Final = chr(0x2013)  # EN DASH; written as a code point for ruff RUF001
_HHMM: Final = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$", re.ASCII)


def _minutes(hhmm: str) -> int:
    hours, _, minutes = hhmm.partition(":")
    return int(hours) * 60 + int(minutes)


def _label(minute: int) -> str:
    day, rest = divmod(minute, _DAY_MIN)
    return f"{_DAYS[day]} {rest // 60:02d}:{rest % 60:02d}"


def _cover(windows: Sequence[WindowSpec]) -> list[list[str]]:
    cover: list[list[str]] = [[] for _ in range(_WEEK_MIN)]
    for window in windows:
        start, end = _minutes(window.start), _minutes(window.end)
        length = end - start + (_DAY_MIN if end <= start else 0)
        for day in window.days or _DAYS:
            first = _DAYS.index(day) * _DAY_MIN + start
            for minute in range(first, first + length):
                cover[minute % _WEEK_MIN].append(window.name)
    return cover


def _coverage_issues(cover: list[list[str]]) -> Iterator[str]:
    first = 0
    for minute in range(1, _WEEK_MIN + 1):
        if minute < _WEEK_MIN and cover[minute] == cover[first]:
            continue
        names = cover[first]
        if len(names) != 1:
            span = _label(first) + ("" if minute - 1 == first else f"{_DASH}{_label(minute - 1)}")
            what = "not covered" if not names else f"covered by {', '.join(names)}"
            yield f"minute {span} {what}"
        first = minute


def validate_windows(windows: Sequence[WindowSpec]) -> list[str]:
    """Issues of the week coverage, names and `chat` count (U08-67); empty means valid.

    Minutes are wall-clock (Monday 00:00 = 0, DST ignored); runs of equal issues merge into
    one `<DAY HH:MM>` en-dash `<DAY HH:MM>` message. At most 20 messages: coverage in
    minute order, then the name and `chat` issues, which always keep their room.
    """
    names = [window.name for window in windows]
    duplicates = sorted({n for n in names if names.count(n) > 1})
    name_issues = [f"window name {name} is used more than once" for name in duplicates]
    chats = names.count("chat")
    if chats != 1:
        name_issues.append(f"exactly one window must be named chat, found {chats}")
    name_issues = name_issues[:_MAX_ISSUES]
    coverage = list(_coverage_issues(_cover(windows)))  # room is kept for the name issues
    return coverage[: _MAX_ISSUES - len(name_issues)] + name_issues


def _crons(cfg: HernessConfig) -> Iterator[tuple[str, str, str]]:
    """(key path, file, cron text) of every cron 08 schedules, in U08-99 step 2 order."""
    schedule = cfg.resilience.schedule
    for i, job in enumerate(schedule.jobs):
        yield f"schedule.jobs[{i}].cron", "resilience.yaml", job.cron
    yield "schedule.rekey.cron", "resilience.yaml", schedule.rekey.cron
    for name, source in cfg.sources.enabled_sources():
        if source.schedule is not None:
            yield f"sources.{name}.schedule", "sources.yaml", source.schedule
        yield f"sources.{name}.reconcile.schedule", "sources.yaml", source.reconcile.schedule


def _cron_issue(text: str, tz: ZoneInfo) -> str | None:
    try:
        CronExpr.parse(text).next_after(clock.now(), tz)
    except ConfigError as exc:
        return exc.message
    return None


def validate_resilience_config(cfg: HernessConfig, *, offline: bool = True) -> list[ConfigIssue]:
    """The `resilience` owner validator (U08-99); one `error` issue per problem, never raises.

    `offline` is accepted for the R-71 `OwnerValidator` protocol; every check is local.
    """
    del offline  # every check is local
    issues = [
        ConfigIssue("error", "schedule.windows", message, "resilience.yaml")
        for message in validate_windows(cfg.resilience.schedule.windows)
    ]
    tz = clock.zone(cfg.weights.business_timezone)  # already checked by the weights model
    for path, file, text in _crons(cfg):
        if (message := _cron_issue(text, tz)) is not None:
            issues.append(ConfigIssue("error", path, message, file))
    if _HHMM.fullmatch(cfg.backup.nightly_at) is None:
        rule = "backup.nightly_at must be HH:MM (00:00-23:59)"
        issues.append(ConfigIssue("error", "backup.nightly_at", rule, "herness.yaml"))
    return issues
