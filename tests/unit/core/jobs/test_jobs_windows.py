"""Tests of U08-66, U08-68, U08-69: window_at, next_window_allowing, preempt_deadline."""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import structlog
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core.errors import ConfigError, SchemaViolation
from herness.core.jobs import windows as w
from herness.core.resilience.settings import ResilienceConfig, WindowSpec

pytestmark = pytest.mark.unit

LONDON = ZoneInfo("Europe/London")
MONDAY = datetime(2026, 1, 5, tzinfo=LONDON)  # a week without a DST change

type UseWindows = Callable[..., None]


@pytest.fixture
def use_windows(monkeypatch: pytest.MonkeyPatch, cfg_default: ResilienceConfig) -> UseWindows:
    """Point `windows.get_config` at a stub: given windows (default §7) in Europe/London."""

    def use(windows: list[WindowSpec] | None = None, tz: str = "Europe/London") -> None:
        schedule = SimpleNamespace(
            windows=cfg_default.schedule.windows if windows is None else windows
        )
        stub = SimpleNamespace(
            resilience=SimpleNamespace(schedule=schedule),
            weights=SimpleNamespace(business_timezone=tz),
        )
        monkeypatch.setattr(w, "get_config", lambda: stub)

    use()
    return use


def _at(day: int, hour: int, minute: int = 0, *, base: datetime = MONDAY) -> datetime:
    """Local London wall clock `day` days after `base`, as aware UTC."""
    return (base + timedelta(days=day)).replace(hour=hour, minute=minute).astimezone(UTC)


def _expected(weekday: int, hour: int) -> str:
    """Design 08 §5.10 default table, written independently of the config."""
    if 6 <= hour < 8:
        return "morning_prep"
    if 8 <= hour < 19:
        return "chat"
    if 19 <= hour < 21:
        return "enrichment"
    started_sunday = (weekday == 6 and hour >= 21) or (weekday == 0 and hour < 6)
    return "deep" if started_sunday else "reviews"


# --- UT08-75 --------------------------------------------------------------------------------


@pytest.mark.usefixtures("use_windows")
def test_ut08_75_every_hour_of_default_week() -> None:
    """UT08-75 each of the 168 hours of a week maps to the design 08 §5.10 window."""
    seen = []
    for hour in range(168):
        day, hh = divmod(hour, 24)
        active = w.window_at(_at(day, hh))
        assert active.spec.name == _expected(day, hh), (day, hh)
        assert active.start_at <= _at(day, hh) < active.end_at
        assert active.start_at.tzinfo is UTC
        assert active.end_at.tzinfo is UTC
        seen.append(active.spec.name)
    assert len(seen) == 168
    assert set(seen) == {"morning_prep", "chat", "enrichment", "reviews", "deep"}


@pytest.mark.usefixtures("use_windows")
def test_ut08_75_sunday_2359_is_deep() -> None:
    """UT08-75 23:59 Sunday is inside `deep` (Sun 21:00 to Mon 06:00)."""
    active = w.window_at(_at(6, 23, 59))
    assert active.spec.name == "deep"
    assert active.start_at == _at(6, 21)
    assert active.end_at == _at(7, 6)


@pytest.mark.usefixtures("use_windows")
def test_ut08_75_bounds_and_saturday_night() -> None:
    """UT08-75 window bounds are the local start and end; Saturday night is `reviews`."""
    chat = w.window_at(_at(1, 8))
    assert (chat.spec.name, chat.start_at, chat.end_at) == ("chat", _at(1, 8), _at(1, 19))
    sat = w.window_at(_at(6, 3))
    assert (sat.spec.name, sat.start_at, sat.end_at) == ("reviews", _at(5, 21), _at(6, 6))
    before = w.window_at(_at(1, 7, 59))
    assert before.spec.name == "morning_prep"


@pytest.mark.usefixtures("use_windows")
def test_ut08_75_dst_nights_resolve_in_local_time() -> None:
    """UT08-75 across DST nights the reviews window still ends at 06:00 local."""
    spring = datetime(2026, 3, 28, tzinfo=LONDON)  # Sat; clocks go forward Sun 01:00
    short = w.window_at(_at(1, 3, base=spring))
    assert short.spec.name == "reviews"
    assert (short.start_at, short.end_at) == (_at(0, 21, base=spring), _at(1, 6, base=spring))
    assert short.end_at - short.start_at == timedelta(hours=8)
    autumn = datetime(2026, 10, 24, tzinfo=LONDON)  # Sat; clocks go back Sun 02:00
    night = w.window_at(_at(1, 3, base=autumn))
    assert night.spec.name == "reviews"
    assert night.end_at - night.start_at == timedelta(hours=10)
    assert night.end_at == _at(1, 6, base=autumn)


def test_ut08_75_naive_now_rejected() -> None:
    """UT08-75 a naive `now` raises SchemaViolation."""
    with pytest.raises(SchemaViolation):
        w.window_at(datetime(2026, 1, 5, 10))  # noqa: DTZ001 - the error under test


def _spec(name: str, start: str, end: str, **extra: object) -> WindowSpec:
    return WindowSpec.model_validate(
        {"name": name, "start": start, "end": end, "classes": ["reasoning"], **extra}
    )


def test_ut08_75_uncovered_minute_uses_next_hour(use_windows: UseWindows) -> None:
    """UT08-75 no window at `now` → the window at `now + 1 hour`; none there → ConfigError."""
    use_windows([_spec("chat", "10:30", "10:00")])
    active = w.window_at(_at(0, 10, 15))
    assert (active.spec.name, active.start_at) == ("chat", _at(0, 10, 30))
    use_windows([_spec("chat", "12:00", "10:00")])
    with pytest.raises(ConfigError):
        w.window_at(_at(0, 10, 15))


def test_ut08_75_overlap_takes_latest_start(use_windows: UseWindows) -> None:
    """UT08-75 several matching windows → the one with the latest `start_at`."""
    use_windows([_spec("chat", "08:00", "20:00"), _spec("wide", "06:00", "05:00")])
    assert w.window_at(_at(0, 9)).spec.name == "chat"
    assert w.window_at(_at(0, 7)).spec.name == "wide"


def test_ut08_75_real_config_wiring(tmp_path: Path, fake_keyring: MemoryKeyring) -> None:
    """UT08-75 with the loaded config (§7 defaults, its business time zone) 10:00 is chat."""
    del fake_keyring
    c.reset_config()
    try:
        cfg = c.init_config("local", config_dir=write_full_config(tmp_path), env={})
        tz = ZoneInfo(cfg.weights.business_timezone)
        now = datetime(2026, 1, 6, 10, tzinfo=tz)
        assert w.window_at(now).spec.name == "chat"
        assert w.window_at(now.replace(hour=22)).spec.name == "reviews"
    finally:
        c.reset_config()


# --- UT08-76 --------------------------------------------------------------------------------


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_decider_loaded_before_morning_prep() -> None:
    """UT08-76 decider loaded 05:00 Tue, at 07:01 → 07:00 (06:00 + 60 reviews overrun)."""
    assert w.preempt_deadline("decider", _at(1, 5), _at(1, 7, 1)) == _at(1, 7)


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_large_switched_in_deep_window() -> None:
    """UT08-76 large switched in-job 22:00 Sun, at 06:30 Mon → 07:00 Mon (deep overrun 60)."""
    assert w.preempt_deadline("large", _at(6, 22), _at(7, 6, 30)) == _at(7, 7)


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_reasoning_in_chat_not_preempted() -> None:
    """UT08-76 reasoning at 08:00 → None (chat allows reasoning)."""
    assert w.preempt_deadline("reasoning", _at(0, 6), _at(1, 8)) is None


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_none_class_and_in_window_switch() -> None:
    """UT08-76 class `none` → None; a class loaded inside the active window → None."""
    assert w.preempt_deadline("none", _at(0, 1), _at(1, 7)) is None
    assert w.preempt_deadline("decider", _at(1, 6, 10), _at(1, 7)) is None


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_hard_start_has_no_overrun() -> None:
    """UT08-76 decider running into chat (hard start) → deadline is 08:00 itself."""
    assert w.preempt_deadline("decider", _at(1, 5), _at(1, 8, 30)) == _at(1, 8)


@pytest.mark.usefixtures("use_windows")
def test_ut08_76_enrichment_overrun_applies_after_enrichment() -> None:
    """UT08-76 large loaded Sat 20:00 then in reviews Sat 21:30 → 21:00 + 120 (enrichment)."""
    assert w.preempt_deadline("large", _at(5, 20), _at(5, 21, 30)) == _at(5, 23)


# --- UT08-77 --------------------------------------------------------------------------------


@pytest.mark.usefixtures("use_windows")
def test_ut08_77_decider_on_tuesday_morning() -> None:
    """UT08-77 Tuesday 10:00, class decider → 19:00 the same day."""
    assert w.next_window_allowing("decider", _at(1, 10)) == _at(1, 19)


@pytest.mark.usefixtures("use_windows")
def test_ut08_77_allowed_now_and_none() -> None:
    """UT08-77 class `none` or a class the active window allows → `now`."""
    now = _at(1, 10, 17)
    assert w.next_window_allowing("none", now) == now
    assert w.next_window_allowing("reasoning", now) == now


@pytest.mark.usefixtures("use_windows")
def test_ut08_77_large_waits_for_sunday_deep() -> None:
    """UT08-77 class large on Tuesday → Sunday 21:00 (the `deep` window)."""
    assert w.next_window_allowing("large", _at(1, 10)) == _at(6, 21)


def test_ut08_77_class_never_allowed_warns(use_windows: UseWindows) -> None:
    """UT08-77 no window allows the class within 8 days → now + 1 day and a WARNING."""
    use_windows([_spec("chat", "08:00", "20:00"), _spec("night", "20:00", "08:00")])
    now = _at(1, 10)
    with structlog.testing.capture_logs() as logs:
        assert w.next_window_allowing("large", now) == now + timedelta(days=1)
    assert [(e["event"], e["log_level"]) for e in logs] == [
        ("jobs.window.class_never_allowed", "warning")
    ]
    assert logs[0]["gpu_class"] == "large"
