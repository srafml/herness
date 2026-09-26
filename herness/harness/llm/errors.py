"""Provider exception translation to the spec 00 §7 taxonomy (U05-30).

Design §5.1.2, §6. Messages name only the provider, the exception type and (for a status
error) the HTTP status: never a request body, header or key (ENG §3.4, TH05-15). Both
translators call ``find_egress_block`` first and return a hit as is, so the SDK's wrapping
of a transport failure into ``APIConnectionError`` can never turn an egress refusal into a
retryable error (TH05-13).
"""

from __future__ import annotations

from typing import Final

import anthropic
import openai

from herness.core import time as clock
from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    RateLimited,
)
from herness.core.resilience.classify import parse_retry_after

_MAX_CAUSE_DEPTH: Final = 10


def find_egress_block(exc: BaseException) -> EgressBlocked | None:
    """Walk ``exc``, then ``__cause__`` or ``__context__``, depth <= 10, for an ``EgressBlocked``.

    Returns the first one found, or ``None`` when the chain holds none within the bound.
    """
    current: BaseException | None = exc
    for _ in range(_MAX_CAUSE_DEPTH + 1):
        if current is None:
            return None
        if isinstance(current, EgressBlocked):
            return current
        current = current.__cause__ or current.__context__
    return None


def _status(exc: BaseException) -> int | None:
    if isinstance(exc, openai.APIStatusError | anthropic.APIStatusError):
        return exc.status_code
    return None


def _message(provider: str, exc: BaseException) -> str:
    status = _status(exc)
    tail = f" HTTP {status}" if status is not None else ""
    return f"{provider} call failed: {type(exc).__name__}{tail}"


def _retry_after(exc: openai.RateLimitError | anthropic.RateLimitError) -> float | None:
    return parse_retry_after(exc.response.headers, clock.now())


def _translate(
    provider: str,
    exc: BaseException,
    *,
    rate_limit: type[BaseException],
    unavailable: tuple[type[BaseException], ...],
    config_error: tuple[type[BaseException], ...],
    auth_error: tuple[type[BaseException], ...],
) -> HernessError:
    blocked = find_egress_block(exc)
    if blocked is not None:
        return blocked
    if isinstance(exc, rate_limit):
        return RateLimited(_message(provider, exc), retry_after=_retry_after(exc))  # type: ignore[arg-type]
    status = _status(exc)
    if isinstance(exc, unavailable) or (status is not None and (status == 529 or status >= 500)):  # noqa: PLR2004
        return ModelUnavailable(_message(provider, exc))
    if isinstance(exc, config_error):
        return ConfigError(_message(provider, exc))
    if isinstance(exc, auth_error):
        return AuthError(_message(provider, exc))
    return ModelUnavailable(_message(provider, exc))


def translate_openai_error(exc: openai.OpenAIError) -> HernessError:
    """Map an ``openai`` exception to the taxonomy (design §5.1.2, §6)."""
    return _translate(
        "openai",
        exc,
        rate_limit=openai.RateLimitError,
        # APITimeoutError subclasses APIConnectionError in this SDK, so isinstance already
        # matches it; listed once here (not twice) since the two are behaviourally identical.
        unavailable=(openai.APIConnectionError, openai.InternalServerError),
        config_error=(
            openai.BadRequestError,
            openai.NotFoundError,
            openai.UnprocessableEntityError,
        ),
        auth_error=(openai.AuthenticationError, openai.PermissionDeniedError),
    )


def translate_anthropic_error(exc: anthropic.AnthropicError) -> HernessError:
    """Map an ``anthropic`` exception to the taxonomy (design §5.1.2, §6)."""
    return _translate(
        "anthropic",
        exc,
        rate_limit=anthropic.RateLimitError,
        # APITimeoutError subclasses APIConnectionError in this SDK too; see the openai
        # translator above for why it is not listed twice.
        unavailable=(
            anthropic.APIConnectionError,
            anthropic.InternalServerError,
        ),
        config_error=(
            anthropic.BadRequestError,
            anthropic.NotFoundError,
            anthropic.UnprocessableEntityError,
        ),
        auth_error=(anthropic.AuthenticationError, anthropic.PermissionDeniedError),
    )
