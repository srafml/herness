"""The httpx and httpx2 exception classes of U08-16 rules 2 and 3 (T08-04b).

Egress clients use `httpx2` (T10-17), whose exceptions are not subclasses of the `httpx`
ones, so each rule names both twins. Only exception classes are imported here."""

from __future__ import annotations

from typing import Final

import httpx
import httpx2

# Rule 2: an HTTP status error; both carry `.response.status_code`, `.headers` and `.text`.
HTTP_STATUS_ERRORS: Final = (httpx.HTTPStatusError, httpx2.HTTPStatusError)
# Rule 3: `TimeoutException` is exactly Connect/Read/Write/PoolTimeout in both libraries.
HTTP_UNAVAILABLE_ERRORS: Final = (
    httpx.ConnectError,
    httpx.TimeoutException,
    httpx.RemoteProtocolError,
    httpx2.ConnectError,
    httpx2.TimeoutException,
    httpx2.RemoteProtocolError,
)
