"""Pure checks of the egress guard: URL shape (step 3) and PII re-scan (step 6) (U10-51).

Size-forced private sibling of ``herness.core.egress`` (w07-s10 ruling), which leaves room
there for the guarded transports and clients of T10-17. Results are reason codes and
per-type counts only; no text ever leaves these functions (TH10-22).
"""

from __future__ import annotations

import ipaddress
import json
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Final
from unicodedata import normalize

import httpx

from herness.core.redact import Redactor
from herness.core.settings import PIN_CLASSES, DeployConfig

__all__ = ["MAX_SCAN_CHARS", "host_reason", "pinned_registries", "registry_host", "rescan"]

MAX_SCAN_CHARS: Final = 1_000_000
# U+200B-U+200D, U+2060, U+FEFF (spec) plus soft hyphen, Mongolian vowel separator and the
# invisible math operators U+2061-U+2064 (fail-closed widening, T10-16 fix round 1).
_INVISIBLE: Final = "\u00ad\u180e\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff"
_ZERO_WIDTH: Final = dict.fromkeys(map(ord, _INVISIBLE))

type Hits = dict[str, int]


def host_reason(url: httpx.URL, allowed: frozenset[str]) -> str | None:
    """Step 3 on a parsed URL: the first failing reason code, or None."""
    if url.scheme != "https":
        return "scheme_not_https"
    if url.port not in {None, 443}:
        return "port_not_443"
    if url.userinfo:
        return "userinfo_present"
    try:
        ipaddress.ip_address(url.host)
    except ValueError:
        return None if url.host.lower() in allowed else "host_not_allowed"
    return "ip_literal"


def registry_host(ref: str) -> str:
    """Registry of an image ref: its first path segment with ``.`` or ``:``, else Docker Hub."""
    first, slash, _rest = ref.partition("/")
    return first.lower() if slash and ("." in first or ":" in first) else "registry-1.docker.io"


def pinned_registries(deploy: DeployConfig) -> frozenset[str]:
    """Registry hosts of the pinned ``deploy.*.image`` refs (step 3 in a download window)."""
    return frozenset(registry_host(getattr(deploy, name).image) for name in PIN_CLASSES)


class _Pairs(list[tuple[str, object]]):
    """A JSON object kept as its key/value pairs, so a duplicate key cannot hide a value."""


def _strings(text: str) -> Iterator[str]:
    """String leaves and object keys of a JSON container, else the whole text."""
    try:
        data = json.loads(text, object_pairs_hook=_Pairs)
    except (ValueError, RecursionError):
        data = None
    if not isinstance(data, list):  # a scalar or not JSON: scan the text as sent
        yield text
        if isinstance(data, str):  # a JSON string body: its escapes decoded too
            yield data
        return
    stack: list[object] = [data]
    while stack:
        node = stack.pop()
        if isinstance(node, str):
            yield node
        elif isinstance(node, _Pairs):
            for key, value in node:
                yield key
                stack.append(value)
        elif isinstance(node, list):
            stack.extend(node)


def _counts(redactor: Redactor, value: str) -> Hits:
    """Hits in ``value`` as-is and NFKC-folded without zero-width chars (ASCII: itself)."""
    folded = value if value.isascii() else normalize("NFKC", value).translate(_ZERO_WIDTH)
    found: Hits = {}
    for variant in (value,) if folded == value else (value, folded):
        counts: Hits = {}
        for span in redactor.scan(variant):  # existing pseudonym tokens are protected
            counts[span.type] = counts.get(span.type, 0) + 1
        found = {kind: max(found.get(kind, 0), counts.get(kind, 0)) for kind in found | counts}
    return found


def rescan(
    body: bytes, redactor: Callable[[], Redactor], blocking: frozenset[str]
) -> tuple[str | None, Hits]:
    """Step 6: ``(reason, hits)``; hits are reported only with ``pii_detected``."""
    if not body:
        return None, {}  # nothing to scan: the redactor (and its key) is never fetched
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return "body_not_utf8", {}
    values = Counter(_strings(text))  # evidence repeats keys and labels: scan each once
    if any(len(value) > MAX_SCAN_CHARS for value in values):
        return "string_too_long", {}
    hits: Hits = {}
    for value, times in values.items():
        try:
            found = _counts(redactor(), value)
        except Exception:  # noqa: BLE001 - any scan or key failure is a refusal (fail closed)
            return "scan_failed", {}
        for kind, count in found.items():
            hits[kind] = hits.get(kind, 0) + count * times
    return ("pii_detected", hits) if hits.keys() & blocking else (None, {})
