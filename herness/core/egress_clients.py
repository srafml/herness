"""Loopback and source clients, the shared transport parts of egress (impl 10 U10-59, U10-110).

Part of ``herness.core.egress``, which re-exports the public names (R-06). With ``egress.py``
this is the only module that builds HTTP clients and transports (ENG §2.1, ST10-25). The
stack is ``httpx2``, the one the locked ``anthropic`` and ``openai`` SDKs accept (T10-17
ruling). It also holds the TLS context of the guarded clients (U10-52 step 1), re-exports the
response streams and loopback transports of the private sibling ``_egress_streams``, and is
the only way impl 01 connectors obtain an ``httpx2`` client (``source_http_client``, R-06).
"""

from __future__ import annotations

import json
import ssl
from collections.abc import Mapping
from pathlib import Path
from typing import Final, Literal, cast

import certifi
import httpx2
from pydantic import SecretStr

from herness.core import _egress_source as es
from herness.core import config as _config
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
from herness.core.errors import ConfigError, EgressBlocked

__all__ = ["DECODABLE", "LOOPBACK_HOSTS", "MAX_RESPONSE_BYTES", "AsyncCountingStream"]
__all__ += ["AsyncLoopbackOnlyTransport", "CountingStream", "LoopbackOnlyTransport"]
__all__ += ["SourceHostTransport", "StreamCounter", "aloopback_http_client", "check_timeout"]
__all__ += ["loopback_http_client", "loopback_refusal", "open_body", "provider_usage"]
__all__ += ["source_http_client", "tls_context"]

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


# --- source client (U10-110, R-06); non-constructing helpers live in ``_egress_source`` ------


class SourceHostTransport(httpx2.BaseTransport):
    """Refuse a source request outside its allowlists before the inner transport (U10-110).

    Checked again for every request, including an absolute URL or a response-supplied next
    link: no redirect is auto-followed (the client is built with ``follow_redirects=False``).
    """

    def __init__(
        self,
        inner: httpx2.BaseTransport,
        *,
        source: str,
        source_hosts: frozenset[str],
        allowed_hosts: frozenset[str],
        max_response_bytes: int,
    ) -> None:
        self._inner, self._source = inner, source
        self._source_hosts, self._allowed_hosts = source_hosts, allowed_hosts
        self._limit = max_response_bytes

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        """Send ``request``; ``EgressBlocked`` for a disallowed host or a large response."""
        host = (request.url.host or "").lower()
        reason = es.source_refusal(request.url, self._source_hosts, self._allowed_hosts)
        if reason is not None:
            raise es.blocked(self._source, host, reason)
        response = self._inner.handle_request(request)
        try:
            counter, read = open_body(response, self._limit, 0, es.body_error)
        except EgressBlocked:
            response.close()
            raise
        if not read:
            stream = cast(httpx2.SyncByteStream, response.stream)
            response.stream = CountingStream(stream, counter, lambda _c: None)
        return response

    def close(self) -> None:
        """Close the inner transport."""
        self._inner.close()


def source_http_client(  # noqa: PLR0913 - spec signature (U10-110)
    source: str,
    base_url: str,
    *,
    timeout_s: float,
    auth: httpx2.Auth | None = None,
    verify: Literal[True] | Path = True,
    max_connections: int = 4,
    max_response_bytes: int = 104_857_600,
    headers: Mapping[str, str] | None = None,
) -> httpx2.Client:
    """The only way an impl 01 source connector obtains an ``httpx2`` client (U10-110, R-06).

    Every request, including one with an absolute URL or a response-supplied next link, is
    refused before the inner transport unless its host is in both the source allowlist and
    the process allowlist, over ``https`` (``http`` only to a loopback host), with no user
    info. TLS verification cannot be disabled. No redirects; ``trust_env=False``. A response
    past ``max_response_bytes`` raises ``EgressBlocked("source response too large", ...)``.
    Source traffic writes no egress line: it is on-network, not egress (design 10 §3.5).
    """
    cfg = _config.get_config()
    section = es.enabled_source(cfg, source)
    es.check_timeout_s(timeout_s)
    es.check_int(max_connections, 1, 64, "max_connections")
    es.check_int(max_response_bytes, 1, es.MAX_SOURCE_RESPONSE_BYTES, "max_response_bytes")
    ca_path = es.check_verify(verify, source)
    try:
        url = httpx2.URL(base_url)
    except (httpx2.InvalidURL, ValueError, TypeError):
        url = httpx2.URL()
    base_host = (url.host or "").lower()
    source_hosts = es.source_allowlist(source, section, base_host)
    allowed_hosts = es.process_allowlist(cfg)
    reason = es.source_refusal(url, source_hosts, allowed_hosts)
    if reason is not None:
        raise es.blocked(source, base_host, reason)
    ssl_ctx = ssl.create_default_context(cafile=str(ca_path) if ca_path else certifi.where())
    ssl_ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ssl_ctx.check_hostname = True
    ssl_ctx.verify_mode = ssl.CERT_REQUIRED
    proxy = cfg.security.network.http_proxy
    inner = httpx2.HTTPTransport(verify=ssl_ctx, proxy=proxy, retries=0)
    transport = SourceHostTransport(
        inner,
        source=source,
        source_hosts=source_hosts,
        allowed_hosts=allowed_hosts,
        max_response_bytes=max_response_bytes,
    )
    timeout = httpx2.Timeout(timeout_s, connect=min(timeout_s, 10.0))
    limits = httpx2.Limits(
        max_connections=max_connections, max_keepalive_connections=max_connections
    )
    return httpx2.Client(
        base_url=base_url,
        transport=transport,
        follow_redirects=False,
        trust_env=False,
        timeout=timeout,
        limits=limits,
        auth=auth,
        headers=headers,
    )
