"""Anthropic Message Batches helpers, private sibling of ``anthropic_client`` (U05-29).

Every function here receives already-built SDK client callables or objects from its caller;
it never constructs an ``anthropic``, ``httpx`` or ``httpx2`` client or transport (R-06,
ST05-13(a)). Errors and logs carry no key, prompt, body or ticket text (TH05-15).
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from typing import TYPE_CHECKING, Any, Final, Protocol

from herness.core.errors import ConfigError, ModelUnavailable, OutputValidationError
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.core.types import LLMRequest, LLMResponse
    from herness.harness.llm.settings import ClientConfig

BATCH_MAX_WAIT_S: Final = 86_400
_MAX_BATCH_REQUESTS: Final = 10_000
_MAX_RESULTS: Final = 10_000
_MIN_POLL_S: Final = 5.0
_BATCH_ID_RE: Final = re.compile(r"^msgbatch_[A-Za-z0-9]+$")

_log = get_logger("harness.llm")


class _ResultLine(Protocol):
    """The subset of ``MessageBatchIndividualResponse`` this module reads."""

    custom_id: str
    result: Any


def check_batch_supported(cfg: ClientConfig) -> None:
    """Precondition: ``cfg.supports.batch``, checked before any network call."""
    if not cfg.supports.batch:
        msg = f"client {cfg.name} does not support the Message Batches API"
        raise ConfigError(msg, client=cfg.name)


def build_batch_requests(
    reqs: Sequence[LLMRequest], build_params: Callable[[LLMRequest], dict[str, object]]
) -> tuple[list[dict[str, object]], dict[str, LLMRequest]]:
    """Validate size and unique ``request_key``; return the SDK requests and pending map."""
    if not 1 <= len(reqs) <= _MAX_BATCH_REQUESTS:
        msg = f"batch must have 1-{_MAX_BATCH_REQUESTS} requests, got {len(reqs)}"
        raise ConfigError(msg)
    pending: dict[str, LLMRequest] = {}
    sdk_requests: list[dict[str, object]] = []
    for req in reqs:
        key = req.metadata.request_key
        if key in pending:
            msg = f"duplicate request_key in batch: {key}"
            raise ConfigError(msg)
        pending[key] = req
        sdk_requests.append({"custom_id": key, "params": build_params(req)})
    return sdk_requests, pending


def validate_batch_id(batch_id: str) -> None:
    """``batch_id`` must match ``^msgbatch_[A-Za-z0-9]+$``."""
    if _BATCH_ID_RE.match(batch_id) is None:
        msg = f"invalid batch id: {batch_id}"
        raise ConfigError(msg)


def validate_poll_interval(poll_s: float) -> None:
    """``poll_s`` must be at least 5 seconds."""
    if poll_s < _MIN_POLL_S:
        msg = f"poll_s must be >= {_MIN_POLL_S}, got {poll_s}"
        raise ConfigError(msg)


def prepare_submit(
    cfg: ClientConfig,
    reqs: Sequence[LLMRequest],
    build_params: Callable[[LLMRequest], dict[str, object]],
) -> tuple[list[dict[str, object]], dict[str, LLMRequest]]:
    """Check every ``submit_batch`` precondition, then build the batch (U05-29)."""
    check_batch_supported(cfg)
    return build_batch_requests(reqs, build_params)


def validate_collect(cfg: ClientConfig, batch_id: str, poll_s: float) -> None:
    """Check every ``collect_batch`` precondition before any network call (U05-29)."""
    check_batch_supported(cfg)
    validate_batch_id(batch_id)
    validate_poll_interval(poll_s)


async def poll_until_ended(
    batch_id: str,
    poll_s: float,
    *,
    retrieve: Callable[..., Awaitable[Any]],
    sleep: Callable[[float], Awaitable[None]],
    monotonic: Callable[[], float],
) -> None:
    """Poll ``retrieve(batch_id)`` until ``processing_status == "ended"``.

    Raises ``ModelUnavailable`` once ``monotonic()`` reaches ``BATCH_MAX_WAIT_S`` past the
    first call, measured on the given monotonic clock (algorithm per U05-29).
    """
    deadline = monotonic() + BATCH_MAX_WAIT_S
    while True:
        batch = await retrieve(batch_id)
        if batch.processing_status == "ended":
            return
        if monotonic() >= deadline:
            msg = f"batch {batch_id} not ended after {BATCH_MAX_WAIT_S} s"
            raise ModelUnavailable(msg)
        await sleep(poll_s)


async def collect_results(
    batch_id: str,
    results: AsyncIterator[_ResultLine],
    pending: dict[str, LLMRequest],
    map_message: Callable[[Any, LLMRequest | None], LLMResponse],
) -> tuple[dict[str, LLMResponse], list[str]]:
    """Map ``succeeded`` results; log the rest at WARNING; cap at 10,000 result lines.

    Returns the mapped responses and every ``custom_id`` seen, so the caller can drop all
    of them from its pending dict (succeeded or not) under its own lock.
    """
    out: dict[str, LLMResponse] = {}
    seen: list[str] = []
    count = 0
    close = getattr(results, "close", None)
    try:
        async for line in results:
            count += 1
            if count > _MAX_RESULTS:
                msg = f"batch {batch_id} has more than {_MAX_RESULTS} results"
                raise OutputValidationError(msg, batch_id=batch_id)
            seen.append(line.custom_id)
            result_type = line.result.type
            if result_type != "succeeded":
                _log.warning(
                    "harness.llm.batch_item_failed",
                    batch_id=batch_id,
                    custom_id=line.custom_id,
                    result_type=result_type,
                )
                continue
            out[line.custom_id] = map_message(line.result.message, pending.get(line.custom_id))
    finally:
        if close is not None:
            await close()
    return out, seen
