"""Tests for herness.core.jobs.validate: window coverage and the `resilience` owner validator."""

from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml
from hypothesis import given
from hypothesis import strategies as st
from pydantic import ValidationError
from tests.support.config_tree import RESILIENCE_FIXTURE, write_checked_config

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import jobs
from herness.core.config_view import ConfigIssue
from herness.core.jobs.validate import validate_resilience_config, validate_windows
from herness.core.resilience.settings import ResilienceConfig, WindowSpec

pytestmark = pytest.mark.unit

DASH = chr(0x2013)  # EN DASH
DAYS = ("MON", "TUE", "WED", "THU", "FRI", "SAT", "SUN")
FILES_SOURCE = """\
version: 1
sources:
  files:
    enabled: true
    schedule: "*/0 * * * *"
    entities:
      tickets: {pattern: "*.csv", key_field: [id]}
"""


@pytest.fixture
def cfg(tmp_path: Path) -> c.HernessConfig:
    """Loaded config: design 08 §7 resilience defaults plus one enabled `files` source."""
    config_dir = write_checked_config(tmp_path)
    (config_dir / "sources.yaml").write_text(FILES_SOURCE.replace("*/0", "*/30"), "utf-8")
    return c.load_config("local", config_dir=config_dir)


def _with_windows(cfg: c.HernessConfig, windows: list[WindowSpec]) -> c.HernessConfig:
    schedule = cfg.resilience.schedule.model_copy(update={"windows": windows})
    return cfg.model_copy(
        update={"resilience": cfg.resilience.model_copy(update={"schedule": schedule})}
    )


def _renamed(window: WindowSpec, **update: object) -> WindowSpec:
    return window.model_copy(update=update)


def _paths(issues: list[ConfigIssue]) -> list[str]:
    return [issue.path for issue in issues]


def test_ut08_04_defaults_are_valid(cfg: c.HernessConfig, cfg_default: ResilienceConfig) -> None:
    """UT08-04 the design 08 §7 defaults give no issue (acceptance check)."""
    assert validate_windows(cfg_default.schedule.windows) == []
    assert validate_resilience_config(cfg) == []
    assert validate_resilience_config(cfg, offline=False) == []
    assert jobs.validate_resilience_config is validate_resilience_config


def test_ut08_04_gap_overlap_and_two_chats(cfg: c.HernessConfig) -> None:
    """UT08-04 a 1-minute gap, an overlap and two `chat` windows name their minute ranges."""
    prep, chat, enrichment, reviews, deep = cfg.resilience.schedule.windows
    windows = [
        _renamed(prep, end="08:30"),  # overlaps chat 08:00-08:29 every day
        _renamed(chat, end="18:59"),  # leaves 18:59 uncovered every day
        _renamed(enrichment, name="chat"),
        reviews,
        deep,
    ]
    issues = validate_resilience_config(_with_windows(cfg, windows))
    assert set(_paths(issues)) == {"schedule.windows"}
    assert {issue.file for issue in issues} == {"resilience.yaml"}
    messages = [issue.message for issue in issues]
    assert messages[:2] == [
        f"minute MON 08:00{DASH}MON 08:29 covered by morning_prep, chat",
        "minute MON 18:59 not covered",
    ]
    assert len(messages) == 16  # 7 days x 2 coverage issues, then the two name issues
    assert messages[-2:] == [
        "window name chat is used more than once",
        "exactly one window must be named chat, found 2",
    ]


def test_ut08_04_ranges_merge_across_days_not_week(cfg_default: ResilienceConfig) -> None:
    """UT08-04 runs merge across midnight but not across the Monday 00:00 week boundary."""
    windows = cfg_default.schedule.windows[:4]  # no Sunday `deep` window
    assert validate_windows(windows) == [
        f"minute MON 00:00{DASH}MON 05:59 not covered",
        f"minute SUN 21:00{DASH}SUN 23:59 not covered",
    ]


def _gap_messages(days: tuple[str, ...]) -> list[str]:
    return [
        message
        for day in days
        for message in (
            f"minute {day} 00:01 not covered",
            f"minute {day} 00:03 not covered",
            f"minute {day} 00:05{DASH}{day} 23:59 not covered",
        )
    ]


def test_ut08_04_at_most_twenty_messages(cfg_default: ResilienceConfig) -> None:
    """UT08-04 at most 20 messages: the first coverage issues, name issues always kept."""
    base = cfg_default.schedule.windows[1]
    windows = [
        _renamed(base, name="chat", start="00:00", end="00:01", days=None),
        _renamed(base, name="w1", start="00:02", end="00:03", days=None),
        _renamed(base, name="w2", start="00:04", end="00:05", days=None),
    ]
    assert validate_windows(windows) == _gap_messages(DAYS)[:20]  # 21 found, the last dropped
    windows[1] = _renamed(windows[1], name="chat")
    assert validate_windows(windows) == [
        *_gap_messages(DAYS[:6]),
        "window name chat is used more than once",
        "exactly one window must be named chat, found 2",
    ]
    assert validate_windows([]) == [
        f"minute MON 00:00{DASH}SUN 23:59 not covered",
        "exactly one window must be named chat, found 0",
    ]


def _fixture() -> dict[str, Any]:
    data: dict[str, Any] = yaml.safe_load(RESILIENCE_FIXTURE.read_text("utf-8"))
    return data


@pytest.mark.parametrize(
    ("change", "loc"),
    [
        (lambda d: d["schedule"]["jobs"][0].update(cron="0 19 * *"), ("jobs", 0, "cron")),
        (lambda d: d["schedule"]["jobs"][0]["then"][0].update(kind="foo"), ("then", 0, "kind")),
        (lambda d: d["schedule"]["windows"][2].update(preload="reasoning"), ("windows", 2)),
        (lambda d: d["schedule"]["windows"][2].update(classes=["gpu9"]), ("classes", 0)),
    ],
)
def test_ut08_05_model_rejects_with_path(change: Any, loc: tuple[object, ...]) -> None:
    """UT08-05 4-field cron, kind, preload and class are rejected by the model at their path."""
    data = _fixture()
    change(data)
    with pytest.raises(ValidationError) as info:
        ResilienceConfig.model_validate(data)
    locs = [err["loc"] for err in info.value.errors()]
    assert any(tuple(where[-len(loc) :]) == loc and where[0] == "schedule" for where in locs)


def test_ut08_05_unparsable_crons_reported_with_paths(tmp_path: Path) -> None:
    """UT08-05 `60 * * * *` and a source `*/0 * * * *` pass the model, fail U08-99."""
    data = _fixture()
    data["schedule"]["jobs"][0]["cron"] = "60 * * * *"
    data["schedule"]["rekey"]["cron"] = "0 0 30 2 *"
    config_dir = write_checked_config(tmp_path)
    (config_dir / "resilience.yaml").write_text(yaml.safe_dump({"version": 1, **data}), "utf-8")
    (config_dir / "sources.yaml").write_text(FILES_SOURCE, "utf-8")
    loaded = c.load_config("local", config_dir=config_dir)
    issues = validate_resilience_config(loaded)
    assert [(i.path, i.message, i.file) for i in issues] == [
        (
            "schedule.jobs[0].cron",
            "invalid cron '60 * * * *': minute value out of range 0-59",
            "resilience.yaml",
        ),
        ("schedule.rekey.cron", "cron never fires: 0 0 30 2 *", "resilience.yaml"),
        (
            "sources.files.schedule",
            "invalid cron '*/0 * * * *': minute step must be >= 1",
            "sources.yaml",
        ),
    ]
    assert {issue.severity for issue in issues} == {"error"}


def test_ut08_05_nightly_at_checked(cfg: c.HernessConfig) -> None:
    """UT08-05 a `backup.nightly_at` that is not HH:MM becomes one issue at that path."""
    broken = cfg.model_copy(
        update={"backup": cfg.backup.model_copy(update={"nightly_at": "24:00"})}
    )
    issues = validate_resilience_config(broken)
    assert [(i.severity, i.path, i.file) for i in issues] == [
        ("error", "backup.nightly_at", "herness.yaml")
    ]


def test_ut08_05_registered_owner_validator(cfg: c.HernessConfig) -> None:
    """UT08-05 registered as `resilience` (R-71), U08-99 issues come from run_owner_validators."""
    cv.register_owner_validator("resilience", validate_resilience_config)
    assert cv.run_owner_validators(cfg, offline=True) == []
    _, *rest = cfg.resilience.schedule.windows
    issues = cv.run_owner_validators(_with_windows(cfg, rest), offline=True)
    assert issues
    assert {(i.severity, i.path) for i in issues} == {("error", "schedule.windows")}
    assert issues[0].message == f"minute MON 06:00{DASH}MON 07:59 not covered"


# --- PT08-05 -------------------------------------------------------------------------------


def _hhmm(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


@st.composite
def _daily_windows(draw: st.DrawFn) -> list[WindowSpec]:
    """A daily partition of the day into 1-5 windows, one named chat, maybe perturbed."""
    cuts = sorted(draw(st.sets(st.integers(0, 1439), min_size=2, max_size=6)))
    ends = [*cuts[1:], cuts[0]]
    shift = draw(st.sampled_from([0, 0, 0, -1, 1, 30]))  # nonzero: gap or overlap
    windows = []
    for i, (start, cut) in enumerate(zip(cuts, ends, strict=True)):
        end = (cut + (shift if i == 0 else 0)) % 1440
        if end == start:
            end = (end + 1) % 1440
        name = "chat" if i == 0 else f"w{i}"
        windows.append(
            WindowSpec(name=name, start=_hhmm(start), end=_hhmm(end), classes=["reasoning"])
        )
    return windows


def _count_at(windows: list[WindowSpec], local: datetime) -> int:
    """Local wall-clock lookup mirroring U08-66 (candidate start dates: today and yesterday)."""
    found = 0
    for window in windows:
        for day in (local.date(), local.date() - timedelta(days=1)):
            if window.days is not None and DAYS[day.weekday()] not in window.days:
                continue
            start = datetime.combine(day, datetime.strptime(window.start, "%H:%M").time())  # noqa: DTZ007
            end_day = day + timedelta(days=1) if window.end <= window.start else day
            end = datetime.combine(end_day, datetime.strptime(window.end, "%H:%M").time())  # noqa: DTZ007
            found += start <= local < end
    return found


LOCAL_MINUTES = st.integers(0, 60 * 24 * 366).map(
    lambda m: datetime(2026, 1, 1) + timedelta(minutes=m)  # noqa: DTZ001 - wall clock
)


@given(windows=_daily_windows(), instants=st.lists(LOCAL_MINUTES, min_size=1, max_size=20))
def test_pt08_05_valid_config_has_one_window(
    windows: list[WindowSpec], instants: list[datetime]
) -> None:
    """PT08-05 for windows passing U08-67, exactly one window contains any wall-clock minute."""
    if validate_windows(windows) == []:
        assert all(_count_at(windows, local) == 1 for local in instants)
    else:
        day = [datetime(2026, 1, 5) + timedelta(minutes=m) for m in range(1440)]  # noqa: DTZ001
        assert any(_count_at(windows, local) != 1 for local in day)


def test_pt08_05_default_windows_every_minute(cfg_default: ResilienceConfig) -> None:
    """PT08-05 the default windows put every minute of a week in exactly one window."""
    monday = datetime.combine(date(2026, 1, 5), datetime.min.time())
    windows = cfg_default.schedule.windows
    assert all(_count_at(windows, monday + timedelta(minutes=m)) == 1 for m in range(10080))
