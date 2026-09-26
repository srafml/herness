"""Pure checks of the egress guard: URL shape (step 3) and PII re-scan (step 6) (U10-51).

Size-forced private sibling of ``herness.core.egress`` (T10-16 ruling), which leaves room
there for the guarded transports and clients of T10-17. Results are reason codes and
per-type counts only; no text ever leaves these functions (TH10-22).
"""

from __future__ import annotations

import ipaddress
import json
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterator
from typing import Final

import httpx

from herness.core.redact import RedactionFailed, Redactor

__all__ = ["MAX_SCAN_CHARS", "host_reason", "rescan"]

MAX_SCAN_CHARS: Final = 1_000_000
_ZERO_WIDTH: Final = dict.fromkeys(map(ord, "\u200b\u200c\u200d\u2060\ufeff"))

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
    """Hits per type in ``value`` as-is and NFKC-folded without zero-width characters."""
    folded = unicodedata.normalize("NFKC", value).translate(_ZERO_WIDTH)
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
        except RedactionFailed:
            return "scan_failed", {}
        for kind, count in found.items():
            hits[kind] = hits.get(kind, 0) + count * times
    return ("pii_detected", hits) if hits.keys() & blocking else (None, {})
