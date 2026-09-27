"""Source HTTP layer (impl 01 U01-58 to U01-62; design 01 §6): the client comes from
``herness.core.egress`` (R-06, ENG §2.1); one JSON page per call under the spec 08 page retry
policy, the body capped while read (cursors stay with the caller, so a retry repeats only the
failed page); ``Retry-After`` is parsed by impl 08 (R-70). Messages carry status, method and
URL path only, never query strings, bodies or credentials (TH01-03)."""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import AbstractContextManager, ExitStack, contextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, ClassVar, Final, Literal

import httpx2

import herness
from herness.connectors.settings_base import SourceSettings
from herness.connectors.settings_entities import MonitoringAdapterSettings
from herness.core import egress
from herness.core import errors as e
from herness.core import time as clock
from herness.core.logging import get_logger
from herness.core.resilience.classify import classify, parse_retry_after
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.retry import retry_page

MAX_RESPONSE_BYTES: Final = 67_108_864  # 64 MiB per page
MAX_LINE_BYTES: Final = 1_048_576  # one streamed line
MAX_PAGES_PER_STREAM: Final = 1_000_000
PAGES_METRIC: Final = "herness_connectors_pages_total"
_MAX_POOL: Final = 64
_TOO_LARGE: Final = "response too large"
_LINE_TOO_LARGE: Final = "line too large"
_USER_AGENT: Final = f"herness/{herness.__version__}"
# U01-60 table: exact statuses first, then status classes (status // 100), else "unexpected".
_BY_STATUS: Final[Mapping[int, tuple[type[e.HernessError], str]]] = {
    400: (e.ConfigError, "source rejected request"),
    401: (e.AuthError, "source refused the credentials"),
    403: (e.AuthError, "source refused the credentials"),
    408: (e.SourceUnavailable, "source request timed out"),
    429: (e.RateLimited, "source rate limited"),
    3: (e.SchemaViolation, "unexpected redirect"),
    5: (e.SourceUnavailable, "source unavailable"),
}
_OTHER: Final = (e.SchemaViolation, "unexpected status")

_log = get_logger("connectors.http")
type _Send = Callable[[], AbstractContextManager[httpx2.Response]]


class ForeignHostError(e.SchemaViolation):
    """A response-supplied next URL leaves the source's ``base_url`` host (U01-62, TH01-02)."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("host",)

    def __init__(self, host: str = "?") -> None:
        super().__init__("next URL is not on the source host", host=host)
        self.host = host


class SourceNotFound(e.SchemaViolation):
    """HTTP 404 (U01-62); callers that expect it, such as the Jira bulk changelog, catch it."""

    _extra_attrs: ClassVar[tuple[str, ...]] = ("path",)

    def __init__(self, path: str, *, method: str = "GET") -> None:
        super().__init__(f"source path not found: HTTP 404 {method} {path}")
        self.path = path


def http_client(
    settings: SourceSettings | MonitoringAdapterSettings, *, source: str, max_concurrency: int
) -> httpx2.Client:
    """The egress source client of ``source`` up to its first ``:`` (``monitoring:<tool>`` gives
    ``monitoring``), pool ``min(2 x max_concurrency, 64)``: the one way to a client (U01-58)."""
    name, base_url = source.split(":", 1)[0], settings.base_url
    if base_url is None:
        msg = f"base_url is not set for source {name}"
        raise e.ConfigError(msg)
    verify: Literal[True] | Path = True if settings.verify is None else Path(settings.verify)
    return egress.source_http_client(
        name,
        base_url,
        timeout_s=settings.timeout_s,
        verify=verify,
        max_connections=min(2 * max_concurrency, _MAX_POOL),
        max_response_bytes=MAX_RESPONSE_BYTES,
    )


def _where(response: httpx2.Response) -> tuple[str, str]:
    """``(method, path)`` of the response's request, without the query string."""
    with suppress(RuntimeError):  # a response built without a request
        return response.request.method, response.request.url.path
    return "?", "?"


def map_http_error(response: httpx2.Response, *, now: datetime) -> e.HernessError | None:
    """HTTP status to taxonomy error, ``None`` for 2xx (U01-60, design 01 §6); never raises."""
    status = response.status_code
    if 200 <= status < 300:  # noqa: PLR2004 - HTTP status class
        return None
    method, path = _where(response)
    if status == 404:  # noqa: PLR2004 - HTTP status
        return SourceNotFound(path, method=method)
    cls, reason = _BY_STATUS.get(status) or _BY_STATUS.get(status // 100, _OTHER)
    msg = f"{reason}: HTTP {status} {method} {path}"
    if cls is e.RateLimited:
        return e.RateLimited(msg, retry_after=parse_retry_after(response.headers, now))
    return cls(msg)


@dataclass(frozen=True, slots=True)
class JsonPage:
    """One page (U01-59): decoded JSON ``body`` (validated by the caller); rel → URL ``links``."""

    status: int
    body: object
    headers: httpx2.Headers
    links: Mapping[str, str]


class CursorGuard:
    """Stops endless pagination within one stream (U01-59, TH01-04)."""

    def __init__(self, max_pages: int = MAX_PAGES_PER_STREAM) -> None:
        self.max_pages = max_pages
        self._seen: set[str] = set()

    def step(self, cursor: str) -> None:
        """Record one page's cursor; ``SchemaViolation`` on a repeat or past the page limit."""
        repeated = cursor in self._seen
        if repeated or len(self._seen) >= self.max_pages:
            msg = "pagination cursor repeated" if repeated else "page limit exceeded"
            raise e.SchemaViolation(msg)
        self._seen.add(cursor)


@contextmanager
def _translating(*, later: bool = False) -> Iterator[None]:
    """Transport error → ``classify`` (``later``: ``SourceUnavailable``); size refusal → schema."""
    try:
        yield
    except httpx2.HTTPError as exc:
        if later:
            msg = "export stream interrupted"
            raise e.SourceUnavailable(msg) from exc
        raise classify(exc, family="source") from exc
    except e.EgressBlocked as exc:
        if exc.reason != "response_too_large":
            raise
        raise e.SchemaViolation(_TOO_LARGE) from exc


def _read_capped(response: httpx2.Response) -> bytearray:
    """The body, read chunk by chunk; ``SchemaViolation`` once it passes the page cap."""
    body = bytearray()
    for chunk in response.iter_bytes():
        body += chunk
        if len(body) > MAX_RESPONSE_BYTES:
            raise e.SchemaViolation(_TOO_LARGE)
    return body


def _decode(raw: bytes | bytearray, *, line: bool = False) -> Any:  # noqa: ANN401 - JSON value
    """``json.loads``; a failure (or a streamed line that is not an object) → SchemaViolation."""
    what = "malformed JSON line" if line else "malformed JSON"
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):  # JSONDecodeError and UnicodeDecodeError included
        raise e.SchemaViolation(what) from None
    if line and not isinstance(value, dict):
        raise e.SchemaViolation(what)
    return value


def _links(response: httpx2.Response) -> dict[str, str]:
    """``Link`` header rels → absolute URLs; an unusable link is dropped, never followed."""
    base, links = response.request.url, {}
    for rel, link in response.links.items():
        with suppress(httpx2.InvalidURL, ValueError, TypeError):
            if rel and link.get("url"):
                links[rel] = str(base.join(link["url"]))
    return links


def _split_lines(chunks: Iterator[bytes]) -> Iterator[bytes]:
    """Non-empty lines of a byte stream, each at most ``MAX_LINE_BYTES``."""
    buf = bytearray()
    for chunk in chunks:
        buf += chunk
        while (cut := buf.find(b"\n")) >= 0:
            line = bytes(buf[:cut])
            del buf[: cut + 1]
            if len(line) > MAX_LINE_BYTES:
                raise e.SchemaViolation(_LINE_TOO_LARGE)
            if line.strip():
                yield line
        if len(buf) > MAX_LINE_BYTES:
            raise e.SchemaViolation(_LINE_TOO_LARGE)
    if buf.strip():
        yield bytes(buf)


class SourceHttp:
    """Page fetch with retry, size limits and error mapping (U01-59); thread-safe (client, auth)."""

    def __init__(
        self,
        client: httpx2.Client,
        *,
        breaker_key: str,
        auth: httpx2.Auth | None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._client, self._key, self._auth, self._clock = client, breaker_key, auth, clock

    def get_json(
        self,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> JsonPage:
        """One GET page with retry; its status is 2xx or in ``allow_status``."""
        kw = self._kw(headers, params)
        return retry_page(
            lambda: self._once(lambda: self._client.stream("GET", url, **kw), allow_status),
            source=self._key,
        )

    def post_json(
        self,
        url: str,
        *,
        json_body: object,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        allow_status: frozenset[int] = frozenset(),
    ) -> JsonPage:
        """One POST page with retry; its status is 2xx or in ``allow_status``."""
        kw = self._kw(headers, params) | {"json": json_body}
        return retry_page(
            lambda: self._once(lambda: self._client.stream("POST", url, **kw), allow_status),
            source=self._key,
        )

    def post_form_lines(self, url: str, *, data: Mapping[str, str]) -> Iterator[dict[str, object]]:
        """Streamed form POST, one JSON object per non-empty line (Splunk export); retried until
        the first line is read, later stream errors are ``SourceUnavailable`` without retry."""
        stack, lines, line = retry_page(lambda: self._open_lines(url, data), source=self._key)
        with stack:
            while line is not None:
                yield _decode(line, line=True)
                with _translating(later=True):
                    line = next(lines, None)

    def check_next_url(self, url: str) -> str:
        """``url`` when absolute ``https`` on the ``base_url`` host+port, else ``ForeignHostError``
        (``http`` too when the base is a loopback ``http`` URL: development fakes)."""
        base = self._client.base_url
        own = (base.host or "").lower()
        try:
            target = httpx2.URL(url)
        except (httpx2.InvalidURL, ValueError, TypeError):
            raise ForeignHostError from None
        host = (target.host or "").lower()
        local = base.scheme == "http" and own in egress.LOOPBACK_HOSTS
        scheme_ok = target.scheme == "https" or (local and target.scheme == "http")
        same = bool(own) and host == own and target.port == base.port
        if not (target.is_absolute_url and scheme_ok and same) or target.userinfo:
            raise ForeignHostError(host)
        return url

    def _kw(self, headers: Mapping[str, str] | None, params: object = None) -> dict[str, Any]:
        sent = httpx2.Headers({"User-Agent": _USER_AGENT, "Accept": "application/json"})
        sent.update(headers or {})
        return {"headers": sent, "params": params, "auth": self._auth}

    def _check(self, response: httpx2.Response, allow: frozenset[int] = frozenset()) -> None:
        """Count the response; raise its mapped error unless its status is in ``allow``."""
        status = response.status_code
        labels = {"source": self._key, "status_class": f"{status // 100}xx"}
        record_counter(PAGES_METRIC, component="connectors", labels=labels)
        now = self._clock() if self._clock is not None else clock.now()
        if status not in allow and (err := map_http_error(response, now=now)) is not None:
            raise err

    def _once(self, send: _Send, allow: frozenset[int]) -> JsonPage:
        """One attempt: fault point, request, status mapping, capped read, JSON decode."""
        fault_point("http.page", source=self._key)
        start = clock.monotonic()
        with _translating(), send() as response:
            self._check(response, allow)
            raw, links = _read_capped(response), _links(response)
        status = response.status_code
        body = None if not raw and status in allow else _decode(raw)
        ms = round((clock.monotonic() - start) * 1000, 1)
        fields = {"source": self._key, "status": status, "bytes": len(raw), "elapsed_ms": ms}
        _log.debug("connectors.http.page_fetched", **fields)
        return JsonPage(status, body, response.headers, links)

    def _open_lines(
        self, url: str, data: Mapping[str, str]
    ) -> tuple[ExitStack, Iterator[bytes], bytes | None]:
        """One ``post_form_lines`` attempt up to its first line; the open response moves to
        the returned stack (closed here when the attempt fails)."""
        fault_point("http.page", source=self._key)
        with ExitStack() as stack, _translating():
            stream = self._client.stream("POST", url, data=data, **self._kw(None))
            response = stack.enter_context(stream)
            self._check(response)
            lines = _split_lines(response.iter_bytes())
            first = next(lines, None)
            return stack.pop_all(), lines, first
