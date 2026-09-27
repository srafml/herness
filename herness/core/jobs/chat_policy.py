"""Chat-hours policy: the chat mode now, its model and the next `live` ETA (design 08 §3.6, §5.10).

Impl 08 U08-75 (`chat_policy`), U08-76 (`chat_model_profile`), U08-77 (`chat_next_live_at`).
Fail closed (TH08-07, R-38): `cloud` is returned, and an off-network client named, only
while `herness.core.egress.cloud_chat_allowed(cfg)` holds; every other doubt resolves to a
local mode. The GPU state is only read here, never changed. Every read on the hot path is
cached for 5 s (worker row and service health in `WorkerGpuState`, breaker rows in
`CircuitBreaker`), which keeps `chat_policy` under 5 ms p95 (BT08-09, O08-05).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Final

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.egress import cloud_chat_allowed
from herness.core.errors import ConfigError
from herness.core.jobs.gpu import ALIVE_HEARTBEATS, ALIVE_STATUSES, gpu_state
from herness.core.jobs.ports import require_jobs_backend
from herness.core.jobs.windows import window_at
from herness.core.logging import get_logger
from herness.core.resilience._state import require_chain_registry
from herness.core.resilience.breaker import breaker

if TYPE_CHECKING:
    from herness.core.jobs.ports import WorkerRow
    from herness.core.jobs.windows import ActiveWindow
    from herness.core.resilience.ports import ChainRegistry
    from herness.core.types import ChatMode

__all__ = ["SWAP_ETA_S", "chat_model_profile", "chat_next_live_at", "chat_policy"]

SWAP_ETA_S: Final = 360  # design 08 §8 decider → reasoning swap target (O08-04)
_SEARCH: Final = timedelta(days=8)  # U08-77 walk limit
_CHAT: Final = "chat"
_OFF_HOURS: Final = "chat_off_hours"
_VLLM: Final = "vllm-reasoning"
_REASONING: Final = "reasoning"

_log = get_logger("jobs")


def _first(chains: ChainRegistry, role: str, depth: str, *, off_network: bool) -> str | None:
    """The first key of `chain_for(role, depth)` whose client has this `off_network`."""
    for key in chains.chain_for(role, depth):
        if chains.config(key).off_network is off_network:
            return key
    return None


def _open(key: str) -> bool:
    """Is breaker `model:<key>` open (the cached state, ≤ 5 s old)?"""
    return breaker(f"model:{key}").state() == "open"


def chat_policy(now: datetime) -> ChatMode:
    """The chat mode at `now` (U08-75; design 08 §5.10 pseudocode).

    `live` while the reasoning class is loaded, `vllm-reasoning` is healthy and the local
    chat client's breaker is not open. Otherwise the configured fallback of the window
    (`schedule.chat.in_hours_unavailable` in the `chat` window, else `off_hours`); `cloud`
    degrades to `small_model` unless `cloud_chat_allowed(cfg)` (R-38), and `small_model`
    becomes `defer` while the off-hours client's breaker is open (or there is none).
    Raises ConfigError when `chain_for("chat", "fast")` has no local client.
    """
    chains = require_chain_registry()
    chat_key = _first(chains, _CHAT, "fast", off_network=False)
    if chat_key is None:
        msg = "chat chain has no local client"
        raise ConfigError(msg)
    g = gpu_state()
    if g.loaded_class() == _REASONING and g.service_healthy(_VLLM) and not _open(chat_key):
        return "live"
    cfg = get_config()
    chat = cfg.resilience.schedule.chat
    mode: ChatMode = (
        chat.in_hours_unavailable if window_at(now).spec.name == _CHAT else chat.off_hours
    )
    if mode == "cloud" and not cloud_chat_allowed(cfg):
        mode = "small_model"
    if mode == "small_model":
        small = chains.chain_for(_OFF_HOURS, "fast")
        if not small or _open(small[0]):
            mode = "defer"
    return mode


def chat_model_profile(mode: ChatMode, depth: str = "fast") -> str | None:
    """The client key that answers chat in `mode` (U08-76; design 08 §3.6).

    `live` → first local key of `chain_for("chat", depth)`; `small_model` → first key of
    `chain_for("chat_off_hours", depth)`; `cloud` → first off-network key of
    `chain_for("chat", depth)` (WARNING `jobs.chat.no_cloud_client` and None when there is
    none); `defer` → None. `cloud` also yields None, with WARNING
    `jobs.chat.cloud_not_allowed`, while `cloud_chat_allowed(cfg)` is false (TH08-07).
    """
    if mode == "defer":
        return None
    chains = require_chain_registry()
    if mode == "live":
        return _first(chains, _CHAT, depth, off_network=False)
    if mode == "small_model":
        small = chains.chain_for(_OFF_HOURS, depth)
        return small[0] if small else None
    if not cloud_chat_allowed(get_config()):
        _log.warning("jobs.chat.cloud_not_allowed", depth=depth)
        return None
    key = _first(chains, _CHAT, depth, off_network=True)
    if key is None:
        _log.warning("jobs.chat.no_cloud_client", depth=depth)
    return key


def _alive(row: WorkerRow, now: datetime, heartbeat_s: float) -> bool:
    """The U08-54 liveness rule (the same rule as `gpu._alive`, design 08 §3.4, R-44)."""
    fresh = row.heartbeat_at > now - timedelta(seconds=ALIVE_HEARTBEATS * heartbeat_s)
    return row.status in ALIVE_STATUSES and fresh


def _swapping_to_reasoning() -> bool:
    heartbeat_s = get_config().resilience.resilience.jobs.heartbeat_s
    wall = clock.now()
    return any(
        w.gpu_class_loaded == "swapping"
        and w.requested_class == _REASONING
        and _alive(w, wall, heartbeat_s)
        for w in require_jobs_backend().list_workers()
    )


def _live_capable(window: ActiveWindow) -> bool:
    return window.spec.preload == _REASONING or _REASONING in window.spec.classes


def chat_next_live_at(now: datetime) -> datetime | None:
    """When `live` chat is next expected, aware UTC (U08-77).

    `now + 360 s` while an alive worker swaps to `reasoning`; else the start of the first
    window after the active one whose `preload` or `classes` include `reasoning`, walking
    at most 8 days; None when there is none.
    """
    if _swapping_to_reasoning():
        return (now + timedelta(seconds=SWAP_ETA_S)).astimezone(UTC)
    window = window_at(now)
    limit = now + _SEARCH
    while window.end_at <= limit:
        window = window_at(window.end_at)  # end_at strictly grows, so the walk ends
        if _live_capable(window):
            return window.start_at
    return None
