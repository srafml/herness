"""Loopback clients and the shared transport parts of the egress component (impl 10 U10-59).

Part of ``herness.core.egress``, which re-exports the public names (R-06). With ``egress.py``
this is the only module that builds HTTP clients and transports (ENG §2.1, ST10-25). The
stack is ``httpx2``, the one the locked ``anthropic`` and ``openai`` SDKs accept (T10-17
ruling). It also holds the TLS context of the guarded clients (U10-52 step 1) and re-exports
the response streams and loopback transports of the private sibling ``_egress_streams``.
"""

from __future__ import annotations

import json
import ssl
from typing import Final, cast

import certifi
import httpx2
from pydantic import SecretStr

from herness.core._egress_streams import (
    DECODABLE,
    LOOPBACK_HOSTS,
    MAX_RESPONSE_BYTES,
    AsyncCountingStream,
    AsyncLoopbackOnlyTransport,
    CountingStream,
    LoopbackOnlyTransport,
    StreamCounter,
    loopback_refusal,
    open_body,
)
from herness.core.errors import ConfigError

__all__ = ["DECODABLE", "LOOPBACK_HOSTS", "MAX_RESPONSE_BYTES", "AsyncCountingStream"]
__all__ += ["AsyncLoopbackOnlyTransport", "CountingStream", "LoopbackOnlyTransport"]
__all__ += ["StreamCounter", "aloopback_http_client", "check_timeout", "loopback_http_client"]
__all__ += ["loopback_refusal", "open_body", "provider_usage", "tls_context"]

MAX_TIMEOUT_S: Final = 3_600.0


def tls_context() -> ssl.SSLContext:
    """TLS 1.2 or later, certificate and host name verified against certifi (U10-52 step 1)."""
    ctx = ssl.create_default_context(cafile=certifi.where())
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    return ctx


def check_timeout(timeout: float, name: str) -> None:
    """``ConfigError`` unless ``0 < timeout <= 3600`` (NaN fails too)."""
    if not 0 < timeout <= MAX_TIMEOUT_S:
        msg = f"{name} must be greater than 0 and at most 3600 seconds"
        raise ConfigError(msg)


def _loopback_settings(
    base_url: str, timeout_s: float, bearer: SecretStr | None, max_response_bytes: int
) -> tuple[httpx2.Timeout, dict[str, str]]:
    """Check the U10-59 preconditions; the client timeout and default headers."""
    try:
        url = httpx2.URL(base_url)
    except (httpx2.InvalidURL, ValueError, TypeError):
        url = httpx2.URL()  # no scheme and no host: refused below
    if (refusal := loopback_refusal(url)) is not None:
        raise refusal
    check_timeout(timeout_s, "timeout_s")
    ceiling = max_response_bytes
    if isinstance(ceiling, bool) or not isinstance(ceiling, int):
        ceiling = 0
    if not 0 < ceiling <= MAX_RESPONSE_BYTES:
        msg = "max_response_bytes must be greater than 0 and at most 52428800"
        raise ConfigError(msg)
    headers = {"Accept-Encoding": "identity"}
    if bearer is not None:
        headers["Authorization"] = f"Bearer {bearer.get_secret_value()}"
    return httpx2.Timeout(timeout_s, connect=min(timeout_s, 5.0)), headers


def loopback_http_client(
    base_url: str,
    *,
    timeout_s: float,
    bearer: SecretStr | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx2.Client:
    """The only client for local model servers and their health checks (U10-59, R-06).

    Every request goes to a ``LOOPBACK_HOSTS`` host with ``Accept-Encoding: identity``; a
    response over ``max_response_bytes`` (by ``content-length``, or decoded bytes while it is
    read) raises ``EgressBlocked("response_too_large")``, and an encoding other than identity,
    gzip or deflate ``EgressBlocked("unsupported_encoding")``. No redirects, no environment
    proxies. Timeout: ``connect=min(timeout_s, 5.0)``; read, write and pool ``timeout_s``.
    Local calls write no egress line and pass no redaction re-scan.
    """
    timeout, headers = _loopback_settings(base_url, timeout_s, bearer, max_response_bytes)
    inner = httpx2.HTTPTransport(retries=0)
    return httpx2.Client(
        base_url=base_url,
        transport=LoopbackOnlyTransport(inner, max_response_bytes),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


def aloopback_http_client(
    base_url: str,
    *,
    timeout_s: float,
    bearer: SecretStr | None = None,
    max_response_bytes: int = MAX_RESPONSE_BYTES,
) -> httpx2.AsyncClient:
    """Async twin of ``loopback_http_client`` with the same checks and limits (U10-59)."""
    timeout, headers = _loopback_settings(base_url, timeout_s, bearer, max_response_bytes)
    inner = httpx2.AsyncHTTPTransport(retries=0)
    return httpx2.AsyncClient(
        base_url=base_url,
        transport=AsyncLoopbackOnlyTransport(inner, max_response_bytes),
        headers=headers,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
    )


def _tokens(value: object) -> int | None:
    ok = isinstance(value, int) and not isinstance(value, bool) and value >= 0
    return cast(int, value) if ok else None


def provider_usage(body: bytes | None) -> tuple[int | None, int | None]:
    """``(tokens_in, tokens_out)`` from ``usage``: Anthropic names, else OpenAI-compatible."""
    try:
        data = json.loads(body) if body else None
    except (ValueError, RecursionError):
        data = None
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None
    tokens_in = usage.get("input_tokens", usage.get("prompt_tokens"))
    tokens_out = usage.get("output_tokens", usage.get("completion_tokens"))
    return _tokens(tokens_in), _tokens(tokens_out)
