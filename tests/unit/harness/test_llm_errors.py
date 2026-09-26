"""Tests for herness.harness.llm.errors: translate_openai_error, translate_anthropic_error,
find_egress_block (U05-30)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import anthropic
import httpx2
import openai
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    RateLimited,
)
from herness.harness.llm.errors import (
    find_egress_block,
    translate_anthropic_error,
    translate_openai_error,
)

pytestmark = pytest.mark.unit

_REQ = httpx2.Request("GET", "https://api.example.invalid/v1/chat/completions")
_UNLEAKED_MARKER = "do-not-leak-this-body-text-into-the-message"
_SECRET_BODY = '{"error": {"message": "' + _UNLEAKED_MARKER + '"}}'


@pytest.fixture
def herness_cfg(tmp_path: Path, fake_keyring: MemoryKeyring) -> Iterator[c.HernessConfig]:
    """A full config so ``parse_retry_after``'s default ``max_s`` can read it (U08-17)."""
    c.reset_config()
    yield c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    c.reset_config()


def _resp(status: int, body: str = "", **headers: str) -> httpx2.Response:
    return httpx2.Response(status, request=_REQ, text=body, headers=headers)


# --- UT05-28 openai ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: openai.BadRequestError("x", response=_resp(400), body=None), ConfigError),
        (lambda: openai.NotFoundError("x", response=_resp(404), body=None), ConfigError),
        (lambda: openai.UnprocessableEntityError("x", response=_resp(422), body=None), ConfigError),
        (lambda: openai.AuthenticationError("x", response=_resp(401), body=None), AuthError),
        (lambda: openai.PermissionDeniedError("x", response=_resp(403), body=None), AuthError),
        (lambda: openai.InternalServerError("x", response=_resp(500), body=None), ModelUnavailable),
        (lambda: openai.APIConnectionError(request=_REQ), ModelUnavailable),
        (lambda: openai.APITimeoutError(request=_REQ), ModelUnavailable),
        (lambda: openai.APIStatusError("x", response=_resp(529), body=None), ModelUnavailable),
        (lambda: openai.ConflictError("x", response=_resp(409), body=None), ModelUnavailable),
        (lambda: openai.APIStatusError("x", response=_resp(418), body=None), ModelUnavailable),
    ],
    ids=[
        "bad_request_400",
        "not_found_404",
        "unprocessable_422",
        "authentication_401",
        "permission_denied_403",
        "internal_server_500",
        "connection_error",
        "timeout_error",
        "status_529",
        "conflict_409_is_other",
        "unmapped_418_is_other",
    ],
)
def test_ut05_28_translate_openai_error(exc: object, expected: type[HernessError]) -> None:
    """UT05-28 each openai exception maps to its taxonomy class per the U05-30 table."""
    built: openai.OpenAIError = exc()  # type: ignore[operator]
    result = translate_openai_error(built)
    assert type(result) is expected
    assert "openai call failed" in result.message
    assert type(built).__name__ in result.message


def test_ut05_28_rate_limit_carries_retry_after(herness_cfg: c.HernessConfig) -> None:
    """UT05-28 openai RateLimitError maps to RateLimited with the parsed retry-after."""
    exc = openai.RateLimitError("x", response=_resp(429, **{"Retry-After": "5"}), body=None)
    result = translate_openai_error(exc)
    assert isinstance(result, RateLimited)
    assert result.retry_after == pytest.approx(5.0)


def test_ut05_28_message_never_carries_the_body() -> None:
    """UT05-28 message names only the provider, exception type and HTTP status: no body."""
    exc = openai.BadRequestError("x", response=_resp(400, _SECRET_BODY), body=None)
    result = translate_openai_error(exc)
    assert _UNLEAKED_MARKER not in result.message
    assert result.message == "openai call failed: BadRequestError HTTP 400"


def test_ut05_28_egress_block_cause_returned_as_is() -> None:
    """UT05-28 an EgressBlocked wrapped by the SDK's transport error is returned as is."""
    blocked = EgressBlocked("refused", egress_id="egr_1", reason="off_network")
    try:
        raise blocked
    except EgressBlocked as cause:
        wrapper = openai.APIConnectionError(request=_REQ)
        wrapper.__cause__ = cause
    result = translate_openai_error(wrapper)
    assert result is blocked


# --- UT05-35 anthropic ---------------------------------------------------------------------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (lambda: anthropic.BadRequestError("x", response=_resp(400), body=None), ConfigError),
        (lambda: anthropic.NotFoundError("x", response=_resp(404), body=None), ConfigError),
        (
            lambda: anthropic.UnprocessableEntityError("x", response=_resp(422), body=None),
            ConfigError,
        ),
        (lambda: anthropic.AuthenticationError("x", response=_resp(401), body=None), AuthError),
        (lambda: anthropic.PermissionDeniedError("x", response=_resp(403), body=None), AuthError),
        (
            lambda: anthropic.InternalServerError("x", response=_resp(500), body=None),
            ModelUnavailable,
        ),
        (lambda: anthropic.APIConnectionError(request=_REQ), ModelUnavailable),
        (lambda: anthropic.APITimeoutError(request=_REQ), ModelUnavailable),
        (lambda: anthropic.OverloadedError("x", response=_resp(529), body=None), ModelUnavailable),
        (
            lambda: anthropic.ServiceUnavailableError("x", response=_resp(503), body=None),
            ModelUnavailable,
        ),
        (
            lambda: anthropic.DeadlineExceededError("x", response=_resp(504), body=None),
            ModelUnavailable,
        ),
        (lambda: anthropic.ConflictError("x", response=_resp(409), body=None), ModelUnavailable),
        (
            lambda: anthropic.RequestTooLargeError("x", response=_resp(413), body=None),
            ModelUnavailable,
        ),
    ],
    ids=[
        "bad_request_400",
        "not_found_404",
        "unprocessable_422",
        "authentication_401",
        "permission_denied_403",
        "internal_server_500",
        "connection_error",
        "timeout_error",
        "overloaded_529",
        "service_unavailable_503",
        "deadline_exceeded_504",
        "conflict_409_is_other",
        "request_too_large_413_is_other",
    ],
)
def test_ut05_35_translate_anthropic_error(exc: object, expected: type[HernessError]) -> None:
    """UT05-35 each anthropic exception (including status 529) maps per the U05-30 table."""
    built: anthropic.AnthropicError = exc()  # type: ignore[operator]
    result = translate_anthropic_error(built)
    assert type(result) is expected
    assert "anthropic call failed" in result.message
    assert type(built).__name__ in result.message


def test_ut05_35_rate_limit_carries_retry_after(herness_cfg: c.HernessConfig) -> None:
    """UT05-35 anthropic RateLimitError maps to RateLimited with the parsed retry-after."""
    exc = anthropic.RateLimitError("x", response=_resp(429, **{"Retry-After": "7"}), body=None)
    result = translate_anthropic_error(exc)
    assert isinstance(result, RateLimited)
    assert result.retry_after == pytest.approx(7.0)


def test_ut05_35_egress_block_cause_returned_as_is() -> None:
    """UT05-35 an EgressBlocked wrapped by the SDK's transport error is returned as is."""
    blocked = EgressBlocked("refused", egress_id="egr_2", reason="off_network")
    try:
        raise blocked
    except EgressBlocked as cause:
        wrapper = anthropic.APIConnectionError(request=_REQ)
        wrapper.__cause__ = cause
    result = translate_anthropic_error(wrapper)
    assert result is blocked


# --- find_egress_block (acceptance check) ----------------------------------------------


def test_ut05_35_find_egress_block_finds_a_nested_cause_at_depth_10() -> None:
    """UT05-35 acceptance: find_egress_block finds an EgressBlocked nested exactly 10 deep."""
    blocked = EgressBlocked("refused", egress_id="egr_3", reason="off_network")
    current: BaseException = blocked
    for i in range(10):
        wrapper = RuntimeError(f"wrap {i}")
        wrapper.__cause__ = current
        current = wrapper
    assert find_egress_block(current) is blocked


def test_ut05_35_find_egress_block_none_when_absent() -> None:
    """UT05-35 a chain with no EgressBlocked anywhere returns None."""
    current: BaseException = RuntimeError("root")
    for i in range(5):
        wrapper = RuntimeError(f"wrap {i}")
        wrapper.__cause__ = current
        current = wrapper
    assert find_egress_block(current) is None


def test_ut05_35_find_egress_block_beyond_depth_is_not_found() -> None:
    """UT05-35 an EgressBlocked past depth 10 is not found (bounded walk)."""
    blocked = EgressBlocked("refused", egress_id="egr_4", reason="off_network")
    current: BaseException = blocked
    for i in range(12):
        wrapper = RuntimeError(f"wrap {i}")
        wrapper.__cause__ = current
        current = wrapper
    assert find_egress_block(current) is None


def _wrap_via_context(blocked: EgressBlocked) -> RuntimeError:
    """Raise ``blocked``, then raise a plain error while handling it (implicit ``__context__``)."""
    message = "during handling"
    try:
        raise blocked
    except EgressBlocked:
        try:
            raise RuntimeError(message) from None  # noqa: TRY301 - building a chain, not an abstraction
        except RuntimeError as wrapper:
            return wrapper


def test_ut05_35_find_egress_block_uses_context_when_no_cause() -> None:
    """UT05-35 implicit chaining (``__context__``) is walked when ``__cause__`` is unset."""
    blocked = EgressBlocked("refused", egress_id="egr_5", reason="off_network")
    wrapper = _wrap_via_context(blocked)
    assert find_egress_block(wrapper) is blocked


def test_ut05_35_find_egress_block_root_is_itself() -> None:
    """UT05-35 the exception itself, with no chain, is found at depth 0."""
    blocked = EgressBlocked("refused", egress_id="egr_6", reason="off_network")
    assert find_egress_block(blocked) is blocked
