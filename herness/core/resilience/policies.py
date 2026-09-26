"""Retry policies and backoff math (U08-11 to U08-15; design 08 §3.1, §5.2)."""

from __future__ import annotations

import random
import types
import typing
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from tenacity import RetryCallState
from tenacity.wait import wait_base

from herness.core import config as _config
from herness.core.config import config_hash, get_config
from herness.core.errors import ConfigError, RateLimited
from herness.core.resilience._state import process_state
from herness.core.resilience.classify import ErrorFamily
from herness.core.resilience.ports import ClientInfo
from herness.core.types import PolicyName

_POLICY_NAMES: Final = frozenset(typing.get_args(PolicyName.__value__))
# 2.0 ** 1023 is the largest finite power; beyond it every ceiling is `cap_s` anyway.
_MAX_EXPONENT: Final = 1023


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    """One retry policy (U08-11): full-jitter parameters, per-attempt timeouts, Retry-After cap."""

    name: str
    attempts: int
    base_s: float
    cap_s: float
    max_elapsed_s: float
    timeout_s: float | None = None
    connect_timeout_s: float | None = None
    retry_after_cap_s: float = 0.0  # 0 = any positive retry_after re-raises


POLICY_FAMILY: Final[Mapping[PolicyName, ErrorFamily]] = types.MappingProxyType(
    {
        "source_http_page": "source",
        "llm_local": "model",
        "llm_large": "model",
        "llm_cloud": "model",
        "embed_batch": "model",
        "gpu_health": "model",
        "decider_local": "decider",
        "decider_cloud": "decider",
        "tool_store": "store",
        "warehouse_read": "store",
        "sqlite_write": "store",
    }
)

# Callers replace `max_elapsed_s` with the class `start_timeout_s` via `dataclasses.replace`.
GPU_HEALTH_POLICY: Final = RetryPolicy(
    "gpu_health",
    attempts=10000,
    base_s=2,
    cap_s=10,
    max_elapsed_s=900,
    timeout_s=5,
    connect_timeout_s=5,
    retry_after_cap_s=0,
)
# Design 08 §7 `sqlite_write`, used while no config is cached: `run_write` (impl 02 U02-38)
# also runs before any config exists (migrations, tooling), as the interim shim did (T08-07).
SQLITE_WRITE_DEFAULT: Final = RetryPolicy(
    "sqlite_write", attempts=6, base_s=0.2, cap_s=5, max_elapsed_s=30
)


def _build_cache() -> dict[str, RetryPolicy]:
    policies = get_config().resilience.resilience.retry.policies
    return {
        name: RetryPolicy(
            name,
            attempts=s.attempts,
            base_s=float(s.base_s),
            cap_s=float(s.cap_s),
            max_elapsed_s=float(s.max_elapsed_s),
            timeout_s=None if s.timeout_s is None else float(s.timeout_s),
            connect_timeout_s=None if s.connect_timeout_s is None else float(s.connect_timeout_s),
            retry_after_cap_s=float(s.retry_after_cap_s),
        )
        for name, s in policies.items()
    }


def policy(name: PolicyName) -> RetryPolicy:
    """Look up a policy (U08-12); cached per `config_hash`, rebuilt when the config changes."""
    if name not in _POLICY_NAMES:
        msg = f"unknown retry policy: {name!r}"
        raise ConfigError(msg, policy=str(name))
    if name == "gpu_health":
        return GPU_HEALTH_POLICY
    if name == "sqlite_write" and _config._Cache.config is None:
        return SQLITE_WRITE_DEFAULT  # never load a config just for an ops write
    cfg = get_config()
    state = process_state()
    with state.lock:  # the config is frozen: the same object has the same hash (T08-07)
        seen = state.policies_cfg
        same = seen is not None and seen[0] is cfg and seen[1] == state.policies_hash
    h = seen[1] if same and seen is not None else config_hash(cfg)
    with state.lock:
        if state.policies_hash != h or name not in state.policies_cache:
            state.policies_cache = _build_cache()
            state.policies_hash = h
        state.policies_cfg = (cfg, h)
        found = state.policies_cache.get(name)
    if found is None:  # settings validation requires every name; defensive only
        msg = f"retry policy {name!r} missing from resilience.retry.policies"
        raise ConfigError(msg, policy=name)
    return found


def policy_for_client(info: ClientInfo) -> RetryPolicy:
    """Pick the LLM policy for one client (U08-13; design 08 §5.2 table)."""
    if info.off_network:
        return policy("llm_cloud")
    if info.gpu_class == "large":
        return policy("llm_large")
    return policy("llm_local")


def _ceiling(attempt: int, base_s: float, cap_s: float) -> float:
    return min(cap_s, base_s * 2.0 ** min(max(attempt - 1, 0), _MAX_EXPONENT))


def full_jitter_delay(
    attempt: int, p: RetryPolicy, *, retry_after: float | None = None, rng: random.Random
) -> float:
    """Full-jitter sleep after failed `attempt`, honouring `retry_after` up to its cap (U08-14)."""
    jitter = rng.uniform(0, _ceiling(attempt, p.base_s, p.cap_s))
    if retry_after is None:
        return jitter
    return min(max(retry_after, jitter), p.retry_after_cap_s)


class FullJitterRetryAfter(wait_base):
    """Tenacity wait strategy: `full_jitter_delay` with `RateLimited.retry_after` (U08-14)."""

    def __init__(self, p: RetryPolicy, rng: random.Random) -> None:
        self.policy = p
        self.rng = rng

    def __call__(self, retry_state: RetryCallState) -> float:
        outcome = retry_state.outcome
        exc = outcome.exception() if outcome is not None and outcome.failed else None
        retry_after = exc.retry_after if isinstance(exc, RateLimited) else None
        return full_jitter_delay(
            retry_state.attempt_number, self.policy, retry_after=retry_after, rng=self.rng
        )


def job_backoff_delay(attempts: int, *, rng: random.Random) -> float:
    """Delay before the next job attempt (U08-15; design 08 §5.2 "Job" column)."""
    backoff = get_config().resilience.resilience.retry.job_backoff
    return rng.uniform(0, _ceiling(attempts, backoff.base_s, backoff.cap_s))
