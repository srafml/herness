"""Open the source breaker on an authentication failure (impl 01 §6, design 08 §9.2).

Shared by the drivers that map errors outside ``SourceHttp`` (MongoDB, Snowflake): the
failure is one attempt, never retried, and the source's breaker stays open until a probe.
"""

from __future__ import annotations

from herness.core.errors import AuthError, HernessError
from herness.core.resilience.breaker import breaker


def open_on_auth(source: str, err: HernessError) -> HernessError:
    """``err``, after force-opening ``source``'s breaker when it is an ``AuthError``."""
    if isinstance(err, AuthError):
        breaker(source).force_open(err)
    return err
