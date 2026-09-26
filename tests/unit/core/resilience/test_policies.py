"""Tests for herness.core.resilience.policies: policies, jitter and job backoff (T08-04)."""

import dataclasses
import random
import typing
from dataclasses import dataclass

import pytest
import tenacity
from hypothesis import given
from hypothesis import strategies as st

from herness.core import config as c
from herness.core import resilience
from herness.core.errors import ConfigError, ModelUnavailable, RateLimited
from herness.core.resilience import policies as p
from herness.core.resilience._state import ProcessState
from herness.core.types.jobs import PolicyName

pytestmark = pytest.mark.unit

# Design 08 §5.2 table: attempts, base_s, cap_s, max_elapsed_s, timeout_s,
# connect_timeout_s, retry_after_cap_s ("—" is 0; caller-supplied timeouts are None).
TABLE: dict[str, tuple[int, float, float, float, float | None, float | None, float]] = {
    "source_http_page": (6, 1, 60, 600, None, 10, 300),
    "llm_local": (4, 2, 30, 900, None, None, 60),
    "llm_large": (2, 30, 120, 7200, None, None, 60),
    "llm_cloud": (5, 2, 60, 900, None, None, 120),
    "decider_local": (3, 2, 20, 300, 30, None, 30),
    "decider_cloud": (3, 2, 30, 300, 30, None, 120),
    "embed_batch": (3, 2, 20, 300, 120, None, 0),
    "tool_store": (3, 0.5, 4, 30, None, None, 0),
    "warehouse_read": (3, 1, 10, 60, 600, None, 0),
    "sqlite_write": (6, 0.2, 5, 30, None, None, 0),
}


def _fields(pol: p.RetryPolicy) -> tuple[object, ...]:
    return (
        pol.attempts,
        pol.base_s,
        pol.cap_s,
        pol.max_elapsed_s,
        pol.timeout_s,
        pol.connect_timeout_s,
        pol.retry_after_cap_s,
    )


@pytest.mark.usefixtures("herness_cfg", "reset_process_state")
def test_ut08_06_policy_values_equal_design_table() -> None:
    """UT08-06 policy(n) for every configurable name equals the design 08 §5.2 table."""
    names = set(typing.get_args(PolicyName.__value__))
    assert names == {*TABLE, "gpu_health"}
    for name, expected in TABLE.items():
        pol = resilience.policy(typing.cast("PolicyName", name))
        assert pol.name == name
        assert _fields(pol) == expected


@pytest.mark.usefixtures("herness_cfg", "reset_process_state")
def test_ut08_06_gpu_health_is_the_constant() -> None:
    """UT08-06 gpu_health returns GPU_HEALTH_POLICY; callers replace max_elapsed_s."""
    assert p.policy("gpu_health") is p.GPU_HEALTH_POLICY
    assert (
        p.RetryPolicy(
            "gpu_health",
            attempts=10000,
            base_s=2,
            cap_s=10,
            max_elapsed_s=900,
            timeout_s=5,
            connect_timeout_s=5,
            retry_after_cap_s=0,
        )
        == p.GPU_HEALTH_POLICY
    )
    swapped = dataclasses.replace(p.GPU_HEALTH_POLICY, max_elapsed_s=300)
    assert swapped.max_elapsed_s == 300
    with pytest.raises(dataclasses.FrozenInstanceError):
        p.GPU_HEALTH_POLICY.attempts = 1  # type: ignore[misc]


@pytest.mark.usefixtures("herness_cfg")
def test_ut08_06_bogus_name_is_config_error(reset_process_state: ProcessState) -> None:
    """UT08-06 a name outside PolicyName raises ConfigError naming `policy` and the name."""
    with pytest.raises(ConfigError) as exc:
        p.policy("bogus")  # type: ignore[arg-type]
    assert "policy" in exc.value.message
    assert "bogus" in exc.value.message
    assert reset_process_state.policies_cache == {}


def test_ut08_06_cache_built_once_per_config_hash(
    herness_cfg: c.HernessConfig, reset_process_state: ProcessState
) -> None:
    """UT08-06 the cache is built for one config_hash and reused; a new hash rebuilds it."""
    first = p.policy("llm_local")
    assert reset_process_state.policies_hash == c.config_hash(herness_cfg)
    assert set(reset_process_state.policies_cache) == set(TABLE)
    assert p.policy("llm_local") is first
    reset_process_state.policies_hash = "cfg_other"
    rebuilt = p.policy("llm_local")
    assert rebuilt is not first
    assert rebuilt == first
    assert reset_process_state.policies_hash == c.config_hash(herness_cfg)


def test_ut08_06_policy_family_covers_every_name() -> None:
    """UT08-06 POLICY_FAMILY maps every PolicyName to its breaker family (U08-11)."""
    assert set(p.POLICY_FAMILY) == set(typing.get_args(PolicyName.__value__))
    assert p.POLICY_FAMILY["source_http_page"] == "source"
    for name in ("llm_local", "llm_large", "llm_cloud", "embed_batch", "gpu_health"):
        assert p.POLICY_FAMILY[name] == "model"
    assert {p.POLICY_FAMILY["decider_local"], p.POLICY_FAMILY["decider_cloud"]} == {"decider"}
    for name in ("tool_store", "warehouse_read", "sqlite_write"):
        assert p.POLICY_FAMILY[name] == "store"


@dataclass(frozen=True)
class _Info:
    off_network: bool
    gpu_class: str | None


@pytest.mark.usefixtures("herness_cfg", "reset_process_state")
@pytest.mark.parametrize(
    ("info", "expected"),
    [
        (_Info(off_network=True, gpu_class=None), "llm_cloud"),
        (_Info(off_network=True, gpu_class="large"), "llm_cloud"),
        (_Info(off_network=False, gpu_class="large"), "llm_large"),
        (_Info(off_network=False, gpu_class="reasoning"), "llm_local"),
        (_Info(off_network=False, gpu_class=None), "llm_local"),
    ],
)
def test_ut08_07_policy_for_client(info: _Info, expected: str) -> None:
    """UT08-07 off-network → llm_cloud; large → llm_large; reasoning and null → llm_local."""
    assert p.policy_for_client(info).name == expected


def _random_policy(rng: random.Random) -> p.RetryPolicy:
    base = rng.uniform(0.01, 60)
    return p.RetryPolicy(
        "llm_local",
        attempts=5,
        base_s=base,
        cap_s=base + rng.uniform(0, 600),
        max_elapsed_s=900,
        retry_after_cap_s=rng.uniform(0, 600),
    )


def test_pt08_01_full_jitter_bounds_10000_samples() -> None:
    """PT08-01 10 000 samples of random attempt and policy lie within the jitter bounds."""
    rng = random.Random(0)
    for _ in range(10_000):
        pol, attempt = _random_policy(rng), rng.randint(1, 20)
        ceiling = min(pol.cap_s, pol.base_s * 2 ** (attempt - 1))
        assert 0 <= p.full_jitter_delay(attempt, pol, retry_after=None, rng=rng) <= ceiling
        retry_after = rng.uniform(0, pol.retry_after_cap_s)
        got = p.full_jitter_delay(attempt, pol, retry_after=retry_after, rng=rng)
        assert retry_after <= got <= pol.retry_after_cap_s


@given(
    attempt=st.integers(1, 20),
    base=st.floats(0.001, 100),
    extra=st.floats(0, 1000),
    ra_cap=st.floats(0, 1000),
    ra_frac=st.floats(0, 1),
    seed=st.integers(0, 2**32),
)
def test_pt08_01_full_jitter_property(
    attempt: int, base: float, extra: float, ra_cap: float, ra_frac: float, seed: int
) -> None:
    """PT08-01 jitter in [0, min(cap, base·2^(a-1))]; with retry_after ≤ cap in [ra, cap]."""
    pol = p.RetryPolicy(
        "llm_cloud",
        attempts=5,
        base_s=base,
        cap_s=base + extra,
        max_elapsed_s=900,
        retry_after_cap_s=ra_cap,
    )
    rng = random.Random(seed)
    ceiling = min(pol.cap_s, pol.base_s * 2 ** (attempt - 1))
    assert 0 <= p.full_jitter_delay(attempt, pol, retry_after=None, rng=rng) <= ceiling
    retry_after = ra_cap * ra_frac
    got = p.full_jitter_delay(attempt, pol, retry_after=retry_after, rng=rng)
    assert retry_after <= got <= ra_cap


def test_pt08_01_huge_attempt_numbers_do_not_overflow() -> None:
    """PT08-01 gpu_health's 10 000 attempts stay within the cap without OverflowError."""
    rng = random.Random(1)
    for attempt in (1_024, 1_100, 10_000):
        assert 0 <= p.full_jitter_delay(attempt, p.GPU_HEALTH_POLICY, rng=rng) <= 10


def _retry_state(attempt: int, exc: BaseException | None) -> tenacity.RetryCallState:
    state = tenacity.RetryCallState(tenacity.Retrying(), None, (), {})
    state.attempt_number = attempt
    if exc is not None:
        fut = tenacity.Future(attempt)
        fut.set_exception(exc)
        state.outcome = fut
    return state


def test_pt08_01_wait_uses_retry_after_of_rate_limited() -> None:
    """PT08-01 FullJitterRetryAfter reads attempt_number and RateLimited.retry_after only."""
    pol = p.RetryPolicy(
        "llm_cloud", attempts=5, base_s=2, cap_s=60, max_elapsed_s=900, retry_after_cap_s=120
    )
    wait = p.FullJitterRetryAfter(pol, random.Random(0))
    assert isinstance(wait, tenacity.wait.wait_base)
    for _ in range(200):
        assert 100 <= wait(_retry_state(1, RateLimited("slow", retry_after=100))) <= 120
        assert 0 <= wait(_retry_state(1, RateLimited("slow"))) <= 2
        assert 0 <= wait(_retry_state(3, ModelUnavailable("down"))) <= 8
        assert 0 <= wait(_retry_state(9, None)) <= 60


@pytest.mark.usefixtures("herness_cfg")
def test_pt08_02_job_backoff_bounds() -> None:
    """PT08-02 job backoff lies in [0, min(3600, 60·2^(a-1))] (design 08 §7 defaults)."""
    rng = random.Random(0)
    for attempts in range(1, 41):
        ceiling = min(3600, 60 * 2 ** (attempts - 1))
        samples = [p.job_backoff_delay(attempts, rng=rng) for _ in range(250)]
        assert all(0 <= s <= ceiling for s in samples)
        assert max(samples) > ceiling * 0.9
    assert 0 <= p.job_backoff_delay(5_000, rng=rng) <= 3600
