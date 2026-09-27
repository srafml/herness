"""Tests of U08-75 (chat_policy), U08-76 (chat_model_profile), U08-77 (chat_next_live_at)
and the chat part of ST08-07 (TH08-07, R-38) (T08-19)."""

from __future__ import annotations

import itertools
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, get_args
from zoneinfo import ZoneInfo

import pytest
import structlog

import herness.core.jobs.chat_policy as cp
from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.jobs import windows as w
from herness.core.resilience.settings import ResilienceConfig, WindowSpec
from herness.core.types import ChatMode

pytestmark = pytest.mark.unit

LONDON = ZoneInfo("Europe/London")
TUESDAY = datetime(2026, 1, 6, tzinfo=LONDON)  # January: London wall clock == UTC
MODES: tuple[ChatMode, ...] = get_args(ChatMode.__value__)
FALLBACKS = ("small_model", "defer", "cloud")


def _tue(hour: int, minute: int = 0) -> datetime:
    return TUESDAY.replace(hour=hour, minute=minute).astimezone(UTC)


# --- fakes ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Client:
    off_network: bool


@dataclass
class Chains:
    """An in-memory `ChainRegistry`; `config` of an unknown key raises KeyError."""

    chains: dict[tuple[str, str], list[str]]
    clients: dict[str, Client] = field(
        default_factory=lambda: {
            "claude-opus": Client(off_network=True),
            "claude-sonnet": Client(off_network=True),
            "local-30b": Client(off_network=False),
            "local-lora-14b": Client(off_network=False),
            "local-small-cpu": Client(off_network=False),
            "local-small-deep": Client(off_network=False),
        }
    )

    def chain_for(self, model_role: str, depth: str) -> list[str]:
        return list(self.chains.get((model_role, depth), []))

    def config(self, name: str) -> Client:
        return self.clients[name]


def _default_chains() -> Chains:
    return Chains(
        {
            ("chat", "fast"): ["local-30b", "claude-opus"],
            ("chat", "deep"): ["claude-sonnet", "local-lora-14b", "claude-opus"],
            ("chat_off_hours", "fast"): ["local-small-cpu"],
            ("chat_off_hours", "deep"): ["local-small-deep", "local-small-cpu"],
        }
    )


@dataclass
class Gpu:
    loaded: str = "reasoning"
    healthy: bool = True
    asked: list[str] = field(default_factory=list)

    def loaded_class(self) -> str:
        return self.loaded

    def service_healthy(self, name: str) -> bool:
        self.asked.append(name)
        return self.healthy


@dataclass
class Breakers:
    open_keys: set[str] = field(default_factory=set)

    def __call__(self, key: str) -> Any:
        return SimpleNamespace(state=lambda: "open" if key in self.open_keys else "closed")


def _cfg(
    *,
    egress: bool = False,
    profile: str = "local",
    approved: bool = False,
    in_hours: str = "small_model",
    off_hours: str = "small_model",
    purposes: tuple[str, ...] = ("reasoning",),
) -> Any:
    return SimpleNamespace(
        profile=profile,
        security=SimpleNamespace(
            egress=SimpleNamespace(enabled=egress, purposes=list(purposes)),
            data_policy=SimpleNamespace(chat_approved=approved),
        ),
        resilience=SimpleNamespace(
            schedule=SimpleNamespace(
                chat=SimpleNamespace(in_hours_unavailable=in_hours, off_hours=off_hours)
            ),
            resilience=SimpleNamespace(jobs=SimpleNamespace(heartbeat_s=30)),
        ),
    )


@dataclass
class Env:
    mp: pytest.MonkeyPatch
    chains: Chains = field(default_factory=_default_chains)
    gpu: Gpu = field(default_factory=Gpu)
    breakers: Breakers = field(default_factory=Breakers)
    window: str = "chat"

    def install(self, cfg: Any) -> None:
        self.mp.setattr(cp, "require_chain_registry", lambda: self.chains)
        self.mp.setattr(cp, "gpu_state", lambda: self.gpu)
        self.mp.setattr(cp, "breaker", self.breakers)
        self.mp.setattr(cp, "get_config", lambda: cfg)
        window = SimpleNamespace(spec=SimpleNamespace(name=self.window))
        self.mp.setattr(cp, "window_at", lambda now: window)


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> Env:
    return Env(monkeypatch)


# --- UT08-84 ----------------------------------------------------------------------------------


def _oracle(case: tuple[Any, ...]) -> str:
    """Design 08 §5.10 pseudocode with the R-38 gate, written independently of the module."""
    window, loaded, healthy, chat_open, off_open, egress, profile, approved, modes = case
    if loaded == "reasoning" and healthy and not chat_open:
        return "live"
    mode = modes[0] if window == "chat" else modes[1]
    cloud_ok = egress and (profile == "premium" or (profile == "hybrid" and approved))
    if mode == "cloud" and not cloud_ok:
        mode = "small_model"
    if mode == "small_model" and off_open:
        mode = "defer"
    return mode


TABLE = list(
    itertools.product(
        ("chat", "reviews"),  # window
        ("reasoning", "decider", "swapping"),  # loaded class
        (True, False),  # vLLM healthy
        (False, True),  # chat breaker open
        (False, True),  # off-hours breaker open
        (False, True),  # egress enabled (with purpose `reasoning`)
        ("local", "hybrid"),  # profile
        (False, True),  # `chat` approval present
        list(itertools.product(FALLBACKS, FALLBACKS)),  # (in_hours_unavailable, off_hours)
    )
)


def test_ut08_84_full_truth_table(env: Env) -> None:
    """UT08-84 every row of the table matches the design pseudocode; `cloud` never leaks."""
    seen: dict[str, int] = dict.fromkeys(MODES, 0)
    for case in TABLE:
        window, loaded, healthy, chat_open, off_open, egress, profile, approved, modes = case
        env.window, env.gpu = window, Gpu(loaded=loaded, healthy=healthy)
        env.breakers.open_keys = {"model:local-30b"} if chat_open else set()
        if off_open:
            env.breakers.open_keys.add("model:local-small-cpu")
        env.install(
            _cfg(
                egress=egress,
                profile=profile,
                approved=approved,
                in_hours=modes[0],
                off_hours=modes[1],
            )
        )
        got = cp.chat_policy(_tue(10))
        assert got == _oracle(case), case
        seen[got] += 1
        if not egress:
            assert got != "cloud", case
        if profile == "hybrid" and not approved:
            assert got != "cloud", case
        if profile == "local":
            assert got != "cloud", case
    assert len(TABLE) == 2 * 3 * 2 * 2 * 2 * 2 * 2 * 2 * 9
    assert all(count > 0 for count in seen.values()), seen


def test_ut08_84_cloud_needs_every_gate(env: Env) -> None:
    """UT08-84 `cloud` only with egress, purpose `reasoning` and hybrid approval or premium."""
    env.gpu.loaded = "decider"
    for kwargs, expected in [
        ({"egress": True, "profile": "hybrid", "approved": True}, "cloud"),
        ({"egress": True, "profile": "premium"}, "cloud"),
        ({"egress": True, "profile": "premium", "purposes": ()}, "small_model"),
        ({"egress": True, "profile": "hybrid", "approved": False}, "small_model"),
        ({"egress": False, "profile": "premium"}, "small_model"),
        ({"egress": True, "profile": "local", "approved": True}, "small_model"),
        ({"egress": True, "profile": "enterprise", "approved": True}, "small_model"),
    ]:
        env.install(_cfg(in_hours="cloud", off_hours="cloud", **kwargs))
        assert cp.chat_policy(_tue(10)) == expected, kwargs


def test_ut08_84_live_reads_vllm_reasoning_health(env: Env) -> None:
    """UT08-84 `live` checks `vllm-reasoning` health and the first local chat key's breaker."""
    env.chains.chains[("chat", "fast")] = ["claude-opus", "local-lora-14b"]
    env.breakers.open_keys = {"model:local-30b"}  # not the chat key of this chain
    env.install(_cfg())
    assert cp.chat_policy(_tue(10)) == "live"
    assert env.gpu.asked == ["vllm-reasoning"]
    env.breakers.open_keys = {"model:local-lora-14b"}
    assert cp.chat_policy(_tue(10)) == "small_model"


def test_ut08_84_no_local_chat_client_is_config_error(env: Env) -> None:
    """UT08-84 `chain_for("chat", "fast")` without a local client → ConfigError."""
    env.chains.chains[("chat", "fast")] = ["claude-opus"]
    env.install(_cfg())
    with pytest.raises(ConfigError, match="no local client"):
        cp.chat_policy(_tue(10))


def test_ut08_84_empty_off_hours_chain_defers(env: Env) -> None:
    """UT08-84 `small_model` with no `chat_off_hours` client fails closed to `defer`."""
    env.gpu.loaded = "none"
    del env.chains.chains[("chat_off_hours", "fast")]
    env.install(_cfg())
    assert cp.chat_policy(_tue(10)) == "defer"


def test_ut08_84_window_selects_fallback(
    env: Env, monkeypatch: pytest.MonkeyPatch, cfg_default: ResilienceConfig
) -> None:
    """UT08-84 the real `window_at`: `in_hours_unavailable` in `chat`, else `off_hours`."""
    _use_windows(monkeypatch, cfg_default.schedule.windows)
    env.gpu.loaded = "decider"
    env.install(_cfg(in_hours="defer", off_hours="small_model"))
    monkeypatch.setattr(cp, "window_at", w.window_at)
    assert cp.chat_policy(_tue(10)) == "defer"  # chat window 08:00-19:00
    assert cp.chat_policy(_tue(18, 59)) == "defer"
    assert cp.chat_policy(_tue(19)) == "small_model"  # enrichment
    assert cp.chat_policy(_tue(22)) == "small_model"  # reviews
    assert cp.chat_policy(_tue(7)) == "small_model"  # morning_prep


# --- UT08-85 ----------------------------------------------------------------------------------


def test_ut08_85_each_mode_and_depth(env: Env) -> None:
    """UT08-85 first local / chat_off_hours / first off-network / None for each depth."""
    env.install(_cfg(egress=True, profile="premium"))
    expected = {
        ("live", "fast"): "local-30b",
        ("live", "deep"): "local-lora-14b",
        ("small_model", "fast"): "local-small-cpu",
        ("small_model", "deep"): "local-small-deep",
        ("cloud", "fast"): "claude-opus",
        ("cloud", "deep"): "claude-sonnet",
        ("defer", "fast"): None,
        ("defer", "deep"): None,
    }
    for (mode, depth), key in expected.items():
        assert cp.chat_model_profile(mode, depth) == key, (mode, depth)
    assert cp.chat_model_profile("live") == "local-30b"  # depth defaults to fast


def test_ut08_85_cloud_without_off_network_client_warns(env: Env) -> None:
    """UT08-85 `cloud` with no off-network chat client → None, WARNING no_cloud_client."""
    env.chains.chains[("chat", "fast")] = ["local-30b"]
    env.install(_cfg(egress=True, profile="premium"))
    with structlog.testing.capture_logs() as logs:
        assert cp.chat_model_profile("cloud") is None
    events = [(e["event"], e["log_level"]) for e in logs]
    assert ("jobs.chat.no_cloud_client", "warning") in events


def test_ut08_85_cloud_not_allowed_yields_none(env: Env) -> None:
    """UT08-85 `cloud` while `cloud_chat_allowed` is false → None, WARNING (TH08-07)."""
    env.install(_cfg(egress=True, profile="hybrid", approved=False))
    with structlog.testing.capture_logs() as logs:
        assert cp.chat_model_profile("cloud", "deep") is None
    assert [e["event"] for e in logs] == ["jobs.chat.cloud_not_allowed"]


def test_ut08_85_missing_chains_yield_none(env: Env) -> None:
    """UT08-85 no local chat client or no off-hours chain → None (no error)."""
    env.chains = Chains({("chat", "fast"): ["claude-opus"]})
    env.install(_cfg())
    assert cp.chat_model_profile("live") is None
    assert cp.chat_model_profile("small_model") is None


# --- UT08-86 ----------------------------------------------------------------------------------


def _use_windows(monkeypatch: pytest.MonkeyPatch, windows: list[WindowSpec]) -> None:
    stub = SimpleNamespace(
        resilience=SimpleNamespace(schedule=SimpleNamespace(windows=windows)),
        weights=SimpleNamespace(business_timezone="Europe/London"),
    )
    monkeypatch.setattr(w, "get_config", lambda: stub)


@dataclass
class Workers:
    rows: list[Any] = field(default_factory=list)

    def list_workers(self) -> list[Any]:
        return list(self.rows)


def _row(loaded: str, requested: str | None, *, age_s: float = 0, status: str = "running") -> Any:
    return SimpleNamespace(
        gpu_class_loaded=loaded,
        requested_class=requested,
        status=status,
        heartbeat_at=clock.now() - timedelta(seconds=age_s),
    )


@pytest.fixture
def live_env(env: Env, monkeypatch: pytest.MonkeyPatch, cfg_default: ResilienceConfig) -> Workers:
    """Default §7 windows in Europe/London; a jobs backend with scripted worker rows."""
    _use_windows(monkeypatch, cfg_default.schedule.windows)
    env.install(_cfg())
    monkeypatch.setattr(cp, "window_at", w.window_at)
    workers = Workers()
    monkeypatch.setattr(cp, "require_jobs_backend", lambda: workers)
    return workers


def test_ut08_86_tuesday_1930_next_is_reviews(live_env: Workers) -> None:
    """UT08-86 Tue 19:30 (enrichment) → 21:00 Tue (reviews allows reasoning)."""
    live_env.rows = [_row("decider", None)]
    got = cp.chat_next_live_at(_tue(19, 30))
    assert got == _tue(21)
    assert got is not None
    assert got.tzinfo is UTC


def test_ut08_86_tuesday_0300_next_is_morning_prep(live_env: Workers) -> None:
    """UT08-86 Tue 03:00 (inside reviews) → 06:00 (the next window, morning_prep)."""
    assert cp.chat_next_live_at(_tue(3)) == _tue(6)


def test_ut08_86_swap_to_reasoning_in_progress(live_env: Workers) -> None:
    """UT08-86 an alive worker swapping to reasoning → now + 360 s, in UTC."""
    live_env.rows = [_row("decider", None), _row("swapping", "reasoning")]
    now = TUESDAY.replace(hour=19, minute=30)  # a London-aware instant
    got = cp.chat_next_live_at(now)
    assert got == now + timedelta(seconds=360)
    assert got is not None
    assert got.tzinfo is UTC
    assert cp.SWAP_ETA_S == 360


def test_ut08_86_other_swaps_use_windows(live_env: Workers) -> None:
    """UT08-86 swapping to another class, or a dead / stopped worker, is ignored."""
    live_env.rows = [
        _row("swapping", "decider"),
        _row("swapping", "reasoning", age_s=91),  # 3 x heartbeat_s (30) + 1
        _row("swapping", "reasoning", status="stopped"),
    ]
    assert cp.chat_next_live_at(_tue(19, 30)) == _tue(21)
    live_env.rows = [_row("swapping", "reasoning", age_s=89)]
    assert cp.chat_next_live_at(_tue(19, 30)) == _tue(19, 36)


def test_ut08_86_no_reasoning_window_is_none(
    live_env: Workers, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT08-86 no window allows or preloads reasoning within 8 days → None."""
    _use_windows(
        monkeypatch,
        [
            WindowSpec(name="a", start="00:00", end="12:00", classes=["decider"]),
            WindowSpec(name="b", start="12:00", end="00:00", classes=["decider", "large"]),
        ],
    )
    assert cp.chat_next_live_at(_tue(10)) is None


# --- ST08-07 (chat part) ----------------------------------------------------------------------


def test_st08_07_local_profile_never_calls_claude_opus(env: Env) -> None:
    """ST08-07 profile `local`, chain starts with `claude-opus`, mode `cloud` → small_model."""
    env.chains.chains[("chat", "fast")] = ["claude-opus", "local-30b"]
    env.chains.chains[("chat", "deep")] = ["claude-opus", "local-lora-14b"]
    called: list[str] = []

    def call(key: str | None) -> None:
        if key is not None:
            called.append(key)

    for loaded, egress in itertools.product(("decider", "swapping", "none"), (False, True)):
        env.gpu.loaded = loaded
        for window in ("chat", "reviews"):
            env.window = window
            env.install(_cfg(egress=egress, profile="local", in_hours="cloud", off_hours="cloud"))
            mode = cp.chat_policy(_tue(10))
            assert mode == "small_model"
            call(cp.chat_model_profile(mode))
        for mode, depth in itertools.product(MODES, ("fast", "deep")):
            assert cp.chat_model_profile(mode, depth) != "claude-opus", (mode, depth)
    assert called
    assert "claude-opus" not in called
    assert set(called) == {"local-small-cpu"}
