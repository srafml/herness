"""Identifiers, canonical JSON, SHA-256 and query_id (design 00 §5, R-14).

canonical_json, normalize_sql and query_id are the single implementations used by
impl 04 and impl 05. ULIDs are identifiers, not secrets: use new_token for bearer values.
"""

from __future__ import annotations

import datetime
import decimal
import enum
import hashlib
import json
import math
import pathlib
import re
import secrets
import threading
import time
import types
from collections.abc import Mapping
from typing import Final

from herness.core import time as clock
from herness.core.errors import SchemaViolation

CROCKFORD_ALPHABET: Final[str] = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
ULID_LEN: Final[int] = 26
RECORD_KEY_MAX_LEN: Final = 512
_ULID_RE: Final = re.compile(r"[0-7][0-9A-HJKMNP-TV-Z]{25}")
_RAND_BITS: Final = 80
_TS_BITS: Final = 48
_RAND_LIMIT: Final = 1 << _RAND_BITS
_TS_LIMIT: Final = 1 << _TS_BITS
_SOURCE_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,31}")
_ENTITY_RE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")
_BUILD_ID_RE: Final = re.compile(r"\d{8}-\d{6}-[0-9A-HJKMNP-TV-Z]{6}", re.ASCII)
_BUILD_ID_LEN: Final = 22
_FIRST_CONTROL: Final = 32
_DEL_CHAR: Final = 127
_MAX_JSON_DEPTH: Final = 64
_TOKEN_MIN_BYTES: Final = 16
_TOKEN_MAX_BYTES: Final = 64
_WS_RE: Final = re.compile(r"\s+")


class IdKind(enum.StrEnum):
    """The closed set of prefixed ULID identifiers of design 00 §5."""

    RUN = "run"
    TASK = "task"
    JOB = "job"
    FINDING = "finding"
    REC = "rec"
    OUTCOME = "outcome"
    MEMORY = "memory"
    CLUSTER = "cluster"
    ITEM = "item"
    REQUEST = "request"
    EGRESS = "egress"
    AUDIT = "audit"


ID_PREFIXES: Final[Mapping[IdKind, str]] = types.MappingProxyType(
    {
        IdKind.RUN: "run",
        IdKind.TASK: "task",
        IdKind.JOB: "job",
        IdKind.FINDING: "fnd",
        IdKind.REC: "rec",
        IdKind.OUTCOME: "out",
        IdKind.MEMORY: "mem",
        IdKind.CLUSTER: "cl",
        IdKind.ITEM: "rev",
        IdKind.REQUEST: "del",
        IdKind.EGRESS: "egr",
        IdKind.AUDIT: "aud",
    }
)


class _UlidState:
    """Generator state; module state is an accepted ENG §2.3 exception (impl 00 §13.3)."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.last_ms = -1
        self.last_rand = 0


_STATE = _UlidState()


def _encode(value: int) -> str:
    return "".join(CROCKFORD_ALPHABET[(value >> (5 * (25 - i))) & 31] for i in range(ULID_LEN))


def new_ulid() -> str:
    """Return a ULID, strictly increasing within this process.

    Raises SchemaViolation when the timestamp overflows 48 bits (after year 10889).
    """
    ms = time.time_ns() // 1_000_000
    state = _STATE
    with state.lock:
        if ms > state.last_ms:
            rand = secrets.randbits(_RAND_BITS)
        else:
            ms = state.last_ms
            rand = state.last_rand + 1
            if rand >= _RAND_LIMIT:
                ms = state.last_ms + 1
                rand = secrets.randbits(_RAND_BITS)
        state.last_ms = ms
        state.last_rand = rand
    if ms >= _TS_LIMIT:
        msg = "ULID timestamp overflow"
        raise SchemaViolation(msg)
    return _encode((ms << _RAND_BITS) | rand)


def new_id(kind: IdKind) -> str:
    """Return a new prefixed identifier such as ``run_01J8...``. Raises as new_ulid."""
    return ID_PREFIXES[kind] + "_" + new_ulid()


def new_build_id(now: datetime.datetime) -> str:
    """Return a warehouse build_id ``YYYYMMDD-HHMMSS-<ulid6>`` from an aware time.

    Raises SchemaViolation for a naive time.
    """
    u = clock.ensure_utc(now)
    stamp = f"{u.year:04d}{u.month:02d}{u.day:02d}-{u.hour:02d}{u.minute:02d}{u.second:02d}"
    return stamp + "-" + new_ulid()[-6:]


def is_valid_ulid(value: object) -> bool:
    """True iff value is canonical ULID text."""
    return isinstance(value, str) and len(value) == ULID_LEN and bool(_ULID_RE.fullmatch(value))


def is_valid_id(kind: IdKind, value: object) -> bool:
    """True iff value is ``<prefix of kind>_<valid ULID>``."""
    if not isinstance(value, str):
        return False
    prefix = ID_PREFIXES[kind] + "_"
    return value.startswith(prefix) and is_valid_ulid(value[len(prefix) :])


def is_valid_build_id(value: object) -> bool:
    """True iff value is a build_id whose date-time part is a real calendar instant."""
    if not isinstance(value, str) or len(value) != _BUILD_ID_LEN:
        return False
    if _BUILD_ID_RE.fullmatch(value) is None:
        return False
    try:
        datetime.datetime.strptime(value[:15], "%Y%m%d-%H%M%S")  # noqa: DTZ007 - shape check only
    except ValueError:
        return False
    return True


def _valid_key(key: str) -> bool:
    if not 0 < len(key) <= RECORD_KEY_MAX_LEN or key != key.strip():
        return False
    return not any(ord(char) < _FIRST_CONTROL or ord(char) == _DEL_CHAR for char in key)


def _check_parts(source: str, entity: str, source_key: str) -> None:
    msg = "bad record_id part"
    if _SOURCE_RE.fullmatch(source) is None:
        raise SchemaViolation(msg, part="source")
    if _ENTITY_RE.fullmatch(entity) is None:
        raise SchemaViolation(msg, part="entity")
    if not _valid_key(source_key):
        raise SchemaViolation(msg, part="source_key", length=len(source_key))


def make_record_id(source: str, entity: str, source_key: str) -> str:
    """Build ``<source>:<entity>:<source_key>``. Raises SchemaViolation (part, length)."""
    _check_parts(source, entity, source_key)
    return f"{source}:{entity}:{source_key}"


def split_record_id(record_id: str) -> tuple[str, str, str]:
    """Parse a record_id; the source key keeps further colons. Raises SchemaViolation."""
    parts = record_id.split(":", 2)
    if len(parts) != 3:  # noqa: PLR2004 - source, entity, key
        msg = "bad record_id"
        raise SchemaViolation(msg)
    source, entity, key = parts
    _check_parts(source, entity, key)
    return source, entity, key


def _convert_number(value: float | decimal.Decimal) -> object:
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = "non-finite float"
            raise SchemaViolation(msg)
        return value
    if not value.is_finite():
        msg = "non-finite decimal"
        raise SchemaViolation(msg)
    return str(value)


def _convert_leaf(value: object, depth: int) -> object:
    if value is None or isinstance(value, bool | int | str):
        return value
    if isinstance(value, float | decimal.Decimal):
        return _convert_number(value)
    if isinstance(value, datetime.datetime):
        return clock.format_utc(value)
    if isinstance(value, datetime.date):
        return value.isoformat()
    if isinstance(value, pathlib.PurePath):
        return value.as_posix()
    if isinstance(value, enum.Enum):
        return _convert(value.value, depth)
    msg = "unsupported type"
    raise SchemaViolation(msg, type=type(value).__name__)


def _convert(value: object, depth: int) -> object:
    if depth > _MAX_JSON_DEPTH:
        msg = "canonical JSON too deep"
        raise SchemaViolation(msg)
    if isinstance(value, Mapping):
        out: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                msg = "non-string key"
                raise SchemaViolation(msg)
            out[key] = _convert(item, depth + 1)
        return out
    if isinstance(value, list | tuple):
        return [_convert(item, depth + 1) for item in value]
    return _convert_leaf(value, depth)


def canonical_json(value: object) -> str:
    """The one canonical JSON encoding for hashed identifiers (R-14).

    Raises SchemaViolation (type) for unsupported, non-finite, too-deep or naive values.
    """
    return json.dumps(
        _convert(value, 0),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )


def sha256_hex(data: str | bytes) -> str:
    """SHA-256 hex digest; str input is UTF-8 encoded."""
    raw = data.encode("utf-8") if isinstance(data, str) else data
    return hashlib.sha256(raw).hexdigest()


def normalize_sql(sql: str) -> str:
    """Collapse whitespace runs and remove trailing semicolons (design 00 §5)."""
    out = _WS_RE.sub(" ", sql).strip(" ")
    while out.endswith(";"):
        out = out[:-1].rstrip(" ")
    return out


def query_id(sql: str, params: Mapping[str, object], build_id: str) -> str:
    """``q_`` + 16 hex of SHA-256 over canonical {sql, params, build_id} (design 00 §5).

    Raises SchemaViolation for empty SQL, a bad build_id or non-canonicalisable params.
    """
    norm = normalize_sql(sql)
    if not norm:
        msg = "empty SQL for query_id"
        raise SchemaViolation(msg)
    if not is_valid_build_id(build_id):
        msg = "bad build_id for query_id"
        raise SchemaViolation(msg)
    payload = canonical_json({"sql": norm, "params": params, "build_id": build_id})
    return "q_" + sha256_hex(payload)[:16]


def new_token(nbytes: int = 32) -> str:
    """Unguessable URL-safe token for bearer values. Raises SchemaViolation (nbytes)."""
    if not _TOKEN_MIN_BYTES <= nbytes <= _TOKEN_MAX_BYTES:
        msg = "bad token size"
        raise SchemaViolation(msg, nbytes=nbytes)
    return secrets.token_urlsafe(nbytes)
