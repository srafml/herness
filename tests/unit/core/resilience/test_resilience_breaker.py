"""Tests for herness.core.resilience.breaker: state table, per-key breaker, registry, guard and
active probes (impl 08 U08-23..U08-27; T08-06)."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import structlog
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core import resilience
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    HernessError,
    ModelUnavailable,
    QueryError,
    RateLimited,
    SourceUnavailable,
)
from herness.core.resilience import ProcessState, bind_chain_registry, bind_ops_backend
from herness.core.resilience import breaker as bmod
from herness.core.resilience.breaker import (
    CircuitBreaker,
    breaker,
    breaker_transition,
    guard,
    probe_due,
    register_probe,
    run_due_probes,
)
from herness.core.resilience.ports import HealthRow
from herness.core.resilience.settings import BreakerSettings
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

SETTINGS = BreakerSettings(failure_threshold=3, cooldown_s=60, cooldown_max_s=900)
TRANSITIONS = "herness_resilience_breaker_transitions_total"
EMAIL = "ops.person@example.com"


@pytest.fixture
def env(
    ops_db: OpsStoreHandle,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    fake_clock: FakeClock,
    test_redactor: r.Redactor,
) -> Iterator[FakeClock]:
    """Migrated ops store bound as the backend, the full test config, a test redactor and a
    fake clock."""
    del ops_db, fake_keyring, test_redactor
    c.init_config("local", config_dir=write_full_config(tmp_path / "cfgroot"), env={})
    yield fake_clock
    c.reset_config()


def _seed(key: str, row: HealthRow, at: datetime) -> None:
    SqliteResilienceBackend().health_apply(key, lambda _before: row, at)


def _read(key: str) -> HealthRow | None:
    return SqliteResilienceBackend().health_get(key)


def _events(kind: str | None = None) -> list[dict[str, Any]]:
    rows = [dict(r) for r in read_all("SELECT * FROM resilience_event ORDER BY ts, event_id")]
    return [r for r in rows if kind is None or r["kind"] == kind]


@pytest.fixture
def transitions(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str]]:
    """Every transitions-counter increment as (key, to_state), checked for name and component."""
    seen: list[tuple[str, str]] = []

    def spy(name: str, value: float = 1.0, *, component: str, labels: Any = None) -> None:
        assert (name, value, component) == (TRANSITIONS, 1.0, "resilience")
        seen.append((labels["key"], labels["to_state"]))

    monkeypatch.setattr(bmod, "record_counter", spy)
    return seen


# --- UT08-12 / UT08-13: the pure state table ------------------------------------------------

NOW = datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
EARLIER = NOW - timedelta(hours=1)


@dataclass(frozen=True)
class _Case:
    state: str
    failures: int
    trips: int
    event: str
    want_state: str
    want_failures: int
    want_trips: int
    opened_now: bool
    kinds: tuple[str, ...]


CASES = [
    _Case("closed", 0, 0, "failure", "closed", 1, 0, False, ()),
    _Case("closed", 1, 0, "failure", "closed", 2, 0, False, ()),
    _Case("closed", 2, 0, "failure", "open", 3, 1, True, ("breaker_open",)),
    _Case("closed", 1, 0, "force_open", "open", 1, 1, True, ("breaker_open",)),
    _Case("half_open", 0, 2, "force_open", "open", 0, 3, True, ("breaker_open",)),
    _Case("closed", 2, 0, "success", "closed", 0, 0, False, ()),
    _Case("open", 3, 1, "probe_claimed", "half_open", 3, 1, False, ("breaker_half_open",)),
    _Case("half_open", 3, 2, "success", "closed", 0, 0, False, ("breaker_close",)),
    _Case("half_open", 3, 2, "failure", "open", 3, 3, True, ("breaker_open",)),
    _Case("open", 3, 1, "failure", "open", 3, 1, False, ()),
    _Case("open", 3, 1, "success", "open", 3, 1, False, ()),
    _Case("open", 3, 1, "force_open", "open", 3, 1, False, ()),
    _Case("closed", 0, 0, "probe_claimed", "closed", 0, 0, False, ()),
    _Case("half_open", 0, 1, "probe_claimed", "half_open", 0, 1, False, ()),
]


@pytest.mark.parametrize("case", CASES, ids=lambda k: f"{k.state}+{k.event}")
def test_ut08_12_state_table(case: _Case) -> None:
    """UT08-12 every row of design 08 §5.3 plus the no-op rows."""
    opened = EARLIER if case.state in {"open", "half_open"} else None
    row = HealthRow("x", case.state, case.failures, case.trips, opened, "old", EARLIER)  # type: ignore[arg-type]
    out, kinds = breaker_transition(row, case.event, NOW, SETTINGS, key="k", error="boom")  # type: ignore[arg-type]
    assert (out.state, out.failures, out.trips) == (
        case.want_state,
        case.want_failures,
        case.want_trips,
    )
    assert tuple(kinds) == case.kinds
    assert out.source == "k"
    assert out.updated_at == NOW
    assert out.last_error == ("boom" if case.event in {"failure", "force_open"} else "old")
    if case.opened_now:
        assert out.opened_at == NOW
    elif case.want_state == "closed" and case.state == "half_open":
        assert out.opened_at is None
    else:
        assert out.opened_at == opened
    assert out.trips >= 0
    assert out.failures >= 0
    assert out.state != "open" or out.opened_at is not None


def test_ut08_12_missing_row_reads_as_closed() -> None:
    """UT08-12 `None` is closed, 0, 0: a failure counts one, a success writes a clean row."""
    out, kinds = breaker_transition(None, "failure", NOW, SETTINGS, key="jira", error="e")
    assert (out.source, out.state, out.failures, out.trips, kinds) == ("jira", "closed", 1, 0, [])
    single = BreakerSettings(failure_threshold=1, cooldown_s=60, cooldown_max_s=900)
    opened, kinds = breaker_transition(None, "failure", NOW, single, key="jira")
    assert (opened.state, opened.trips, opened.opened_at, kinds) == (
        "open",
        1,
        NOW,
        ["breaker_open"],
    )
    ok, _ = breaker_transition(None, "success", NOW, SETTINGS, key="jira")
    assert (ok.state, ok.failures, ok.last_error) == ("closed", 0, None)


@pytest.mark.parametrize(
    ("trips", "seconds"),
    [(1, 60), (2, 120), (3, 240), (4, 480), (5, 900), (6, 900), (7, 900), (8, 900)],
)
def test_ut08_13_probe_due_backoff(trips: int, seconds: int) -> None:
    """UT08-13 trips 1..8, cooldown 60, max 900 → 60, 120, 240, 480, 900, 900… s."""
    settings = BreakerSettings(failure_threshold=5, cooldown_s=60, cooldown_max_s=900)
    row = HealthRow("k", "open", 5, trips, EARLIER, None, NOW)
    assert probe_due(row, settings) == EARLIER + timedelta(seconds=seconds)


def test_ut08_13_probe_due_without_opened_at_or_trips() -> None:
    """UT08-13 defensive: no `opened_at` uses `updated_at`; trips 0 counts as the first trip."""
    row = HealthRow("k", "open", 0, 0, None, None, NOW)
    assert probe_due(row, SETTINGS) == NOW + timedelta(seconds=60)


# --- UT08-14 / UT08-15: force_open and counting classes -------------------------------------


def test_ut08_14_force_open_auth(env: FakeClock, transitions: list[tuple[str, str]]) -> None:
    """UT08-14 `force_open(AuthError())` → open, trips 1, event detail reason = auth."""
    b = breaker("jira")
    b.force_open(AuthError("token rejected"))
    assert b.state() == "open"
    row = _read("jira")
    assert row is not None
    assert (row.state, row.trips, row.opened_at) == ("open", 1, env.now())
    (event,) = _events("breaker_open")
    detail = event["detail"]
    assert '"reason":"auth"' in detail.replace(" ", "")
    assert '"trips":1' in detail.replace(" ", "")
    assert event["target"] == "jira"
    assert transitions == [("jira", "open")]


def test_ut08_14_force_open_other_reason_and_open_noop(env: FakeClock) -> None:
    """UT08-14 a non-auth force_open names the class; forcing an open breaker emits nothing."""
    del env
    b = breaker("model:local-30b")
    b.force_open(ModelUnavailable("gone"))
    b.force_open(ModelUnavailable("still gone"))
    (event,) = _events("breaker_open")
    assert '"reason":"ModelUnavailable"' in event["detail"].replace(" ", "")
    row = _read("model:local-30b")
    assert row is not None
    assert (row.trips, row.last_error) == (1, "still gone")


def test_ut08_15_only_unavailable_classes_count(env: FakeClock) -> None:
    """UT08-15 QueryError and RateLimited are not counted; ModelUnavailable is."""
    del env
    b = breaker("model:local-30b")
    b.record_failure(QueryError("bad sql"))
    b.record_failure(RateLimited("slow down", retry_after=3.0))
    assert _read("model:local-30b") is None
    b.record_failure(ModelUnavailable("down"))
    row = _read("model:local-30b")
    assert row is not None
    assert (row.state, row.failures, row.last_error) == ("closed", 1, "down")
    breaker("jira").record_failure(SourceUnavailable("jira down"))
    jira = _read("jira")
    assert jira is not None
    assert jira.failures == 1


def test_ut08_15_failures_open_then_success_resets(
    env: FakeClock, transitions: list[tuple[str, str]]
) -> None:
    """UT08-15 five model failures open the breaker; a success after a failure resets it."""
    del env
    b = breaker("model:m1")
    for _ in range(5):
        b.record_failure(ModelUnavailable("down"))
    assert b.state() == "open"
    assert len(_events("breaker_open")) == 1
    assert transitions == [("model:m1", "open")]
    s = breaker("model:m2")
    s.record_failure(ModelUnavailable("blip"))
    s.record_success()
    row = _read("model:m2")
    assert row is not None
    assert (row.state, row.failures) == ("closed", 0)
    assert _events("breaker_close") == []


def test_ut08_15_last_error_redacted_before_cut(env: FakeClock) -> None:
    """UT08-15 (review M-1) redaction runs on the whole text before the 500-char cut: a value
    straddling char 500 is replaced, never left as a fragment."""
    del env
    text = "x" * 488 + EMAIL + " tail"
    breaker("jira").record_failure(SourceUnavailable(text))
    row = _read("jira")
    assert row is not None
    assert row.last_error is not None
    assert len(row.last_error) <= 500
    assert "ops.person" not in row.last_error
    assert row.last_error.startswith("x" * 488)


def test_ut08_15_error_text_redaction_fallbacks(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-15 `last_error` is the class name when redaction fails or returns None."""
    del env

    def boom(_text: str) -> str:
        msg = "redactor unavailable"
        raise ConfigError(msg)

    monkeypatch.setattr(bmod, "redact_text", boom)
    breaker("jira").record_failure(SourceUnavailable("x"))
    monkeypatch.setattr(bmod, "redact_text", lambda _text: None)
    breaker("confluence").record_failure(SourceUnavailable("y" * 900))
    assert [r.last_error for r in (_read("jira"), _read("confluence")) if r] == [
        "SourceUnavailable",
        "SourceUnavailable",
    ]
    monkeypatch.setattr(bmod, "redact_text", lambda text: text)
    breaker("github").record_failure(SourceUnavailable("z" * 900))
    github = _read("github")
    assert github is not None
    assert github.last_error == "z" * 500


# --- UT08-16: the 5 s cache -----------------------------------------------------------------


class _SpyBackend(SqliteResilienceBackend):
    def __init__(self) -> None:
        self.gets = 0
        self.applies = 0

    def health_get(self, key: str) -> HealthRow | None:
        self.gets += 1
        return super().health_get(key)

    def health_apply(self, key: str, fn: Any, now: datetime) -> Any:
        self.applies += 1
        return super().health_apply(key, fn, now)


def test_ut08_16_cache_reads_once_and_writes_through(env: FakeClock) -> None:
    """UT08-16 100 `allow()` within 5 s read once; a transition is written at once."""
    spy = _SpyBackend()
    bind_ops_backend(spy)
    b = breaker("jira")
    for _ in range(100):
        assert b.allow()
        env.advance(0.04)
    b.record_success()
    assert (spy.gets, spy.applies) == (1, 0)
    b.record_failure(SourceUnavailable("down"))
    assert spy.applies == 1
    row = SqliteResilienceBackend().health_get("jira")
    assert row is not None
    assert row.failures == 1
    assert spy.gets == 1
    env.advance(5.0)
    assert b.state() == "closed"
    assert spy.gets == 2


# --- UT08-17 / UT08-18: guard and the registry ----------------------------------------------


def test_ut08_17_guard_raises_with_probe_due(env: FakeClock) -> None:
    """UT08-17 open breaker, probe not due → CircuitOpen with .key and .retry_at == probe_due."""
    key = "model:local-30b"
    breaker(key).force_open(ModelUnavailable("down"))
    opened = env.now()
    env.advance(10)
    with pytest.raises(CircuitOpen) as info:
        guard(key)
    assert info.value.key == key
    assert info.value.retry_at == opened + timedelta(seconds=60)
    assert breaker(key).retry_at() == opened + timedelta(seconds=60)
    guard("jira")  # closed: no error
    assert breaker("jira").retry_at() is None


def test_ut08_17_guard_half_open_uses_cooldown(env: FakeClock) -> None:
    """UT08-17 a fresh half-open breaker has no probe due: retry_at = now + cooldown_s."""
    now = env.now()
    _seed("confluence", HealthRow("confluence", "half_open", 8, 1, now, None, now), now)
    with pytest.raises(CircuitOpen) as info:
        guard("confluence")
    assert info.value.retry_at == now + timedelta(seconds=300)


@pytest.mark.parametrize("key", ["jira", "model:local-30b", "monitoring:datadog"])
def test_ut08_18_valid_keys_share_one_instance(key: str, reset_process_state: ProcessState) -> None:
    """UT08-18 valid keys are accepted and repeat calls return the same instance."""
    first = breaker(key)
    assert isinstance(first, CircuitBreaker)
    assert first.key == key
    assert breaker(key) is first
    assert resilience.breaker(key) is first  # the callable submodule
    assert reset_process_state.breakers[key] is first


@pytest.mark.parametrize("key", ["bad key", "Model:x", "", "model:", "x" * 65, "decider:a b"])
def test_ut08_18_invalid_keys_raise(key: str, reset_process_state: ProcessState) -> None:
    """UT08-18 `bad key`, `Model:x` (and other malformed keys) raise ConfigError."""
    with pytest.raises(ConfigError):
        breaker(key)
    assert reset_process_state.breakers == {}


def test_ut08_18_package_exports(reset_process_state: ProcessState) -> None:
    """UT08-18 the package exports the breaker API; `breaker` is the callable module."""
    del reset_process_state
    assert resilience.breaker is bmod
    assert resilience.CircuitBreaker is CircuitBreaker
    assert resilience.guard is guard
    assert resilience.register_probe is register_probe
    assert resilience.run_due_probes is run_due_probes


# --- UT08-19: half-open staleness and the probe claim ---------------------------------------


@pytest.mark.parametrize(("age", "allowed"), [(601, True), (599, False)])
def test_ut08_19_stale_half_open_is_reclaimed(env: FakeClock, age: int, allowed: bool) -> None:
    """UT08-19 half_open updated 601 s ago → allow() True (re-claimed); at 599 s False."""
    now = env.now()
    past = now - timedelta(seconds=age)
    _seed("jira", HealthRow("jira", "half_open", 8, 1, past, None, past), past)
    assert breaker("jira").allow() is allowed
    row = _read("jira")
    assert row is not None
    assert row.state == "half_open"
    assert row.updated_at == (now if allowed else past)
    assert len(_events("breaker_half_open")) == (1 if allowed else 0)


def test_ut08_19_open_probe_claimed_once(
    env: FakeClock, transitions: list[tuple[str, str]]
) -> None:
    """UT08-19 an open breaker whose probe is due lets exactly one caller through."""
    breaker("model:m").force_open(ModelUnavailable("down"))
    env.advance(59)
    assert not breaker("model:m").allow()
    env.advance(1)
    assert breaker("model:m").allow()
    assert not breaker("model:m").allow()
    assert breaker("model:m").state() == "half_open"
    breaker("model:m").record_success()
    assert breaker("model:m").state() == "closed"
    assert len(_events("breaker_close")) == 1
    assert breaker("model:m").allow()
    assert transitions == [("model:m", "open"), ("model:m", "half_open"), ("model:m", "closed")]
    (half_open,) = _events("breaker_half_open")
    assert '"reason":"probe"' in half_open["detail"].replace(" ", "")


def test_ut08_19_stale_cache_rechecks_probe_due(env: FakeClock) -> None:
    """UT08-19 (review I-1) a row cached before the probe fell due is re-read: another process
    probed, failed and re-opened it (trips 2, 120 s), so this process must not claim."""
    key = "model:m"
    a, b = CircuitBreaker(key), CircuitBreaker(key)  # two processes over one store
    a.force_open(ModelUnavailable("down"))
    env.advance(57)
    assert b.state() == "open"  # B caches the trips-1 row 3 s before the probe is due
    env.advance(3)
    assert a.allow()
    a.record_failure(ModelUnavailable("probe failed"))
    env.advance(1)
    assert b.allow() is False
    row = _read(key)
    assert row is not None
    assert (row.state, row.trips) == ("open", 2)
    assert b.retry_at() == row.opened_at + timedelta(seconds=120)  # type: ignore[operator]
    assert len(_events("breaker_half_open")) == 1
    env.advance(120)
    assert b.allow()
    detail = _events("breaker_half_open")[-1]["detail"].replace(" ", "")
    assert '"trips":2' in detail


def test_ut08_19_lost_claim_rereads_row(env: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-19 a lost probe claim returns False and caches the row another process wrote."""
    breaker("jira").force_open(SourceUnavailable("down"))
    env.advance(301)
    backend = SqliteResilienceBackend()
    bind_ops_backend(backend)
    now = env.now()
    _seed("jira", HealthRow("jira", "half_open", 8, 1, now, None, now), now)
    monkeypatch.setattr(backend, "health_claim_probe", lambda *_a: False)
    b = CircuitBreaker("jira")
    b._cache(HealthRow("jira", "open", 8, 1, now - timedelta(seconds=301), None, now), now)
    assert b.allow() is False
    assert b.state() == "half_open"


def test_ut08_19_cache_ignores_clock_going_back(env: FakeClock) -> None:
    """UT08-19 a cache entry stamped in the future is treated as stale."""
    b = breaker("jira")
    b._cache(HealthRow("jira", "open", 1, 1, env.now(), None, env.now()), env.now() + timedelta(1))
    assert b.state() == "closed"


# --- UT08-104: active probes ----------------------------------------------------------------


class _Chains:
    def __init__(self, off: dict[str, bool]) -> None:
        self.off = off

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        return list(self.off)

    def config(self, name: str) -> Any:
        return SimpleNamespace(off_network=self.off[name])


def _open(key: str, opened: datetime, trips: int = 1) -> None:
    _seed(key, HealthRow(key, "open", 5, trips, opened, None, opened), opened)


def test_ut08_104_run_due_probes(env: FakeClock) -> None:
    """UT08-104 due+success → closed; due+failure → open trips+1; not due untouched; no fn
    → skipped."""
    now = env.now()
    calls: list[str] = []

    def ok(name: str) -> None:
        calls.append(name)

    def down(name: str) -> None:
        calls.append(name)
        msg = "health check failed"
        raise ModelUnavailable(msg)

    register_probe("source", ok)
    register_probe("model", down)
    bind_chain_registry(_Chains({"local-30b": False}))
    _open("jira", now - timedelta(seconds=301))
    _open("model:local-30b", now - timedelta(seconds=61))
    _open("confluence", now - timedelta(seconds=10))
    _open("decider:laya", now - timedelta(hours=1))
    assert run_due_probes(now) == 2
    assert sorted(calls) == ["jira", "local-30b"]
    states = {k: _read(k) for k in ("jira", "model:local-30b", "confluence", "decider:laya")}
    assert {k: (r.state, r.trips) for k, r in states.items() if r} == {
        "jira": ("closed", 0),
        "model:local-30b": ("open", 2),
        "confluence": ("open", 1),
        "decider:laya": ("open", 1),
    }
    assert states["model:local-30b"].opened_at == now  # type: ignore[union-attr]


@pytest.mark.parametrize(
    "error", [AuthError("nope"), RuntimeError("crash"), QueryError("q")], ids=type
)
def test_ut08_104_non_counting_probe_error_reopens(env: FakeClock, error: Exception) -> None:
    """UT08-104 a non-counting or foreign probe error re-opens via a ModelUnavailable
    substitute named after the error class."""
    now = env.now()

    def probe(_name: str) -> None:
        raise error

    register_probe("decider", probe)
    _open("decider:laya", now - timedelta(seconds=61))
    assert run_due_probes(now) == 1
    row = _read("decider:laya")
    assert row is not None
    assert (row.state, row.trips) == ("open", 2)
    assert row.last_error == f"probe failed: {type(error).__name__}"


def test_ut08_104_model_probe_network_rules(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-104 off-network model clients are probed only with egress; an unknown client or
    an unbound registry is skipped (fail closed)."""
    now = env.now()
    calls: list[str] = []
    register_probe("model", calls.append)
    for key in ("model:claude-opus", "model:ghost"):
        _open(key, now - timedelta(seconds=61))
    assert run_due_probes(now) == 0  # no chain registry bound
    bind_chain_registry(_Chains({"claude-opus": True}))
    with structlog.testing.capture_logs() as logs:
        assert run_due_probes(now) == 0  # off-network without egress; ghost unknown
    unknown = [e for e in logs if e["event"] == "resilience.probe.client_unknown"]
    assert [(e["log_level"], e["error_type"]) for e in unknown] == [("debug", "KeyError")]
    real = c.get_config()
    egress_on = SimpleNamespace(
        resilience=real.resilience, security=SimpleNamespace(egress=SimpleNamespace(enabled=True))
    )
    monkeypatch.setattr(bmod, "get_config", lambda: egress_on)
    assert run_due_probes(now) == 1
    assert calls == ["claude-opus"]


def test_ut08_104_probe_due_boundary(env: FakeClock) -> None:
    """UT08-104 (review M-2) a row opened exactly cooldown_s ago is due; 1 s less is not."""
    now = env.now()
    calls: list[str] = []
    register_probe("source", calls.append)
    _open("jira", now - timedelta(seconds=300))
    _open("confluence", now - timedelta(seconds=299))
    assert run_due_probes(now) == 1
    assert calls == ["jira"]


def test_ut08_104_invalid_stored_key_is_skipped(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-104 (review M-3) a `source_health` key failing the key regex is skipped with a
    DEBUG log; the other rows of the tick still run."""
    now = env.now()
    calls: list[str] = []
    register_probe("source", calls.append)
    _open("Bad Key", now - timedelta(hours=1))
    _open("jira", now - timedelta(hours=1))
    with structlog.testing.capture_logs() as logs:
        assert run_due_probes(now) == 1
    assert calls == ["jira"]
    skipped = [e for e in logs if e["event"] == "resilience.probe.invalid_key"]
    assert skipped
    assert skipped[0]["log_level"] == "debug"
    assert "Bad Key" not in str(skipped)


def test_ut08_104_lost_claim_skips_probe(env: FakeClock, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT08-104 a probe claimed by another process is not run."""
    now = env.now()
    backend = SqliteResilienceBackend()
    bind_ops_backend(backend)
    calls: list[str] = []
    register_probe("source", calls.append)
    _open("jira", now - timedelta(seconds=301))
    monkeypatch.setattr(backend, "health_claim_probe", lambda *_a: False)
    assert run_due_probes(now) == 0
    assert calls == []


def test_ut08_104_probe_timeout_counts_as_failure(
    env: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-104 a probe still running after the timeout re-opens with ModelUnavailable."""
    now = env.now()
    release = threading.Event()
    register_probe("source", lambda _name: release.wait(5) and None)
    monkeypatch.setattr(bmod, "PROBE_TIMEOUT_S", 0.05)
    _open("jira", now - timedelta(seconds=301))
    try:
        assert run_due_probes(now) == 1
    finally:
        release.set()
    row = _read("jira")
    assert row is not None
    assert (row.state, row.trips, row.last_error) == ("open", 2, "call timed out after 0.05s")


def test_ut08_104_register_probe_rejects_unknown_prefix(
    reset_process_state: ProcessState,
) -> None:
    """UT08-104 only the source, model and decider prefixes can hold a probe."""
    with pytest.raises(ConfigError):
        register_probe("monitoring", print)  # type: ignore[arg-type]
    register_probe("source", print)
    assert reset_process_state.probes == {"source": print}


def test_ut08_104_call_with_timeout_value_and_error() -> None:
    """UT08-104 the private timeout helper returns the value and re-raises the error."""
    assert bmod._call_with_timeout(lambda: 42, 5) == 42

    def fail() -> None:
        msg = "down"
        raise SourceUnavailable(msg)

    with pytest.raises(SourceUnavailable):
        bmod._call_with_timeout(fail, 5)
    names: list[str] = []
    bmod._call_with_timeout(lambda: names.append(threading.current_thread().name), 5)
    assert names == ["herness-timeout"]
    assert issubclass(SourceUnavailable, HernessError)
