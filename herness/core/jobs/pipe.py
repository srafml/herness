"""Supervisor↔child pipe messages (impl 08 U08-84; design 08 §5.9; TH08-10, TH08-13).

Messages travel as canonical JSON bytes over `Connection.send_bytes` / `recv_bytes`, never as
pickled objects (ENG §3.5). `decode_message` checks the size before parsing and validates with
a strict pydantic discriminated union (`extra="forbid"`); any failure is
`SchemaViolation("bad pipe message")` with no received content in the error.
"""

from __future__ import annotations

import json
from typing import Annotated, Final, Literal, NoReturn

from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError

from herness.core.errors import SchemaViolation
from herness.core.ids import canonical_json
from herness.core.types import GpuClass, ServiceName

__all__ = [
    "MAX_PIPE_MSG_BYTES",
    "GpuOp",
    "GpuReplyMsg",
    "GpuRequestMsg",
    "HeartbeatMsg",
    "OutcomeMsg",
    "PipeMessage",
    "SaveStateMsg",
    "StopMsg",
    "decode_message",
    "encode_message",
]

MAX_PIPE_MSG_BYTES: Final = 8_388_608  # 8 MiB (TH08-10)
REPLY_MESSAGE_CHARS: Final = 500
NOTE_CHARS: Final = 200
ERROR_MESSAGE_CHARS: Final = 2048  # 2 KB
ERROR_CLASS_CHARS: Final = 100  # not in the U08-84 table; a class name, bounded defensively
_ULID_PATTERN: Final = r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$"
_BAD: Final = "bad pipe message"

type GpuOp = Literal["require_class", "service_start", "service_stop", "service_healthy"]


class _Msg(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class StopMsg(_Msg):
    """parent → child: stop the handler (`should_yield` becomes true)."""

    type: Literal["stop"] = "stop"
    reason: Literal["cancel", "preempt", "shutdown"]


class GpuReplyMsg(_Msg):
    """parent → child: result of one `gpu_request`."""

    type: Literal["gpu_reply"] = "gpu_reply"
    request_id: Annotated[str, Field(pattern=_ULID_PATTERN)]
    ok: bool
    error_class: Literal["ModelUnavailable", "ConfigError"] | None = None
    message: Annotated[str, Field(max_length=REPLY_MESSAGE_CHARS)] | None = None
    previous_class: GpuClass | None = None
    healthy: bool | None = None


class HeartbeatMsg(_Msg):
    """child → parent: progress signal; `note` is redacted by the child."""

    type: Literal["heartbeat"] = "heartbeat"
    note: Annotated[str, Field(max_length=NOTE_CHARS)] | None = None


class SaveStateMsg(_Msg):
    """child → parent: job checkpoint (the child enforces the 4 MiB cap)."""

    type: Literal["save_state"] = "save_state"
    state: dict[str, JsonValue]


class GpuRequestMsg(_Msg):
    """child → parent: GPU class swap or in-class service control."""

    type: Literal["gpu_request"] = "gpu_request"
    request_id: Annotated[str, Field(pattern=_ULID_PATTERN)]
    op: GpuOp
    cls: GpuClass | None = None
    service: ServiceName | None = None
    timeout_s: Annotated[float, Field(gt=0, allow_inf_nan=False)] | None = None


class OutcomeMsg(_Msg):
    """child → parent: the handler's final outcome."""

    type: Literal["outcome"] = "outcome"
    status: Literal["done", "yield", "error"]
    result: dict[str, JsonValue]
    error_class: Annotated[str, Field(max_length=ERROR_CLASS_CHARS)] | None = None
    error_message: Annotated[str, Field(max_length=ERROR_MESSAGE_CHARS)] | None = None


type PipeMessage = Annotated[
    StopMsg | GpuReplyMsg | HeartbeatMsg | SaveStateMsg | GpuRequestMsg | OutcomeMsg,
    Field(discriminator="type"),
]

_ADAPTER: Final[TypeAdapter[PipeMessage]] = TypeAdapter(PipeMessage)


def encode_message(m: PipeMessage) -> bytes:
    """Canonical JSON bytes of `m`; over `MAX_PIPE_MSG_BYTES` → `SchemaViolation`."""
    data = canonical_json(m.model_dump()).encode("utf-8")
    if len(data) > MAX_PIPE_MSG_BYTES:
        msg = "pipe message exceeds 8 MiB"
        raise SchemaViolation(msg)
    return data


def _reject_constant(_name: str) -> NoReturn:
    raise ValueError(_BAD)  # NaN / Infinity are not JSON


def decode_message(b: bytes) -> PipeMessage:
    """Validate received bytes (size first, then JSON, then the strict union).

    Any failure → `SchemaViolation("bad pipe message")`; the bytes are never echoed, and the
    parse error is not chained so no content reaches a traceback.
    """
    data: object = b  # a caller may pass anything; only bytes are decoded
    if not isinstance(data, bytes) or len(data) > MAX_PIPE_MSG_BYTES:
        raise SchemaViolation(_BAD)
    try:
        obj = json.loads(data, parse_constant=_reject_constant)
        return _ADAPTER.validate_python(obj, strict=True)
    except (ValueError, RecursionError, ValidationError):
        pass
    raise SchemaViolation(_BAD)
