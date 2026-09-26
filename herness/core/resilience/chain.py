"""Structured-output repair and the model fallback chain (impl 08 U08-35..U08-38; design 08
§3.2, §5.4; TH08-07, TH08-14, TH08-15).

`complete_validated` validates a structured reply and asks the same client to repair it at
most `max_repairs` times. `ModelChain` walks `chain_for(model_role, depth)`: per candidate it
retries under the client's policy (U08-28 async form, trace target `llm`), repairs, and on a
fallback trigger moves on with the original request. Repair error lines carry JSON pointer
paths and validator messages only, never input values (TH08-15). The chain receives model
clients only through `client_for` and never imports `herness.harness` (R-02).
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable, Sequence
from typing import Final

import jsonschema
import tenacity
from pydantic import BaseModel, JsonValue, ValidationError

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelRefused,
    ModelUnavailable,
    OutputValidationError,
)
from herness.core.ids import canonical_json
from herness.core.logging import get_logger
from herness.core.resilience._state import process_state
from herness.core.resilience.breaker import breaker
from herness.core.resilience.events import record_event
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter
from herness.core.resilience.policies import RetryPolicy, policy_for_client
from herness.core.resilience.ports import AsyncCompleter, ChainRegistry, GpuStateReader, TracerLike
from herness.core.resilience.retry import _ainvoke, _areplay, _Retry
from herness.core.types import LLMRequest, LLMResponse, Message, TextPart

__all__ = ("ModelChain", "build_repair_request", "complete_validated")

REPAIRS_METRIC: Final = "herness_resilience_repairs_total"
FALLBACKS_METRIC: Final = "herness_resilience_fallbacks_total"
ECHO_MAX_CHARS: Final = 4000
ERROR_LINES_MAX: Final = 20
ERROR_MSG_MAX_CHARS: Final = 200
MAX_REPAIRS_LIMIT: Final = 5
DEPTHS: Final = frozenset({"fast", "standard", "deep"})
# U08-38 step 2e: the fallback triggers and their `reason`; anything else propagates (2f).
_REASONS: Final[tuple[tuple[type[HernessError], str], ...]] = (
    (ModelUnavailable, "unavailable"),
    (CircuitOpen, "circuit_open"),
    (OutputValidationError, "validation"),
    (ModelRefused, "refusal"),
    (EgressBlocked, "egress_blocked"),
    (AuthError, "auth"),
)
_REPAIR_INTRO: Final = "Your previous reply was not valid. Errors:\n"
_REPAIR_OUTRO: Final = "\nReply with only a JSON object that matches this JSON Schema:\n"
_log: Final = get_logger("resilience")

type _Errors = list[tuple[str, str]]


def _pointer(path: Sequence[object]) -> str:
    """RFC 6901 pointer of a validation path (`~` → `~0`, `/` → `~1`); the root is `/`."""
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _json_of(resp: LLMResponse) -> object:
    """U08-35 step 4: `resp.parsed` if set, else the decoded text."""
    return resp.parsed if resp.parsed is not None else json.loads(resp.text)


def _validate(
    obj: object, schema: dict[str, JsonValue] | None, model: type[BaseModel] | None
) -> _Errors:
    """U08-35 step 5: (pointer, message) pairs; the offending input value is never read."""
    if model is not None:
        try:
            model.model_validate(obj)
        except ValidationError as exc:
            found = exc.errors(include_url=False, include_context=False, include_input=False)
            return [(_pointer(err["loc"]), err["msg"]) for err in found]
        return []
    validator = jsonschema.Draft202012Validator(schema or {})
    return [
        (_pointer(err.absolute_path), f"failed '{err.validator}' constraint")
        for err in validator.iter_errors(obj)
    ]


def _errors_of(
    resp: LLMResponse, req: LLMRequest, name: str, model: type[BaseModel] | None
) -> _Errors:
    """U08-35 steps 3-5: the `llm.output` fault hook, JSON decoding and validation."""
    try:
        fault_point("llm.output", model=name, role=req.metadata.role)
    except OutputValidationError:
        return [("/", "fault: malformed_json")]
    try:
        obj = _json_of(resp)
    except json.JSONDecodeError as exc:
        return [("/", f"invalid JSON: {exc.msg}")]  # `msg` holds no part of the document
    except RecursionError:
        return [("/", "invalid JSON: nesting too deep")]
    except ValueError:  # e.g. an integer too long to convert; its text may quote the input
        return [("/", "invalid JSON: invalid value")]
    return _validate(obj, req.response_schema, model)


def build_repair_request(
    req: LLMRequest, resp: LLMResponse, errors: Sequence[tuple[str, str]]
) -> LLMRequest:
    """The repair request of U08-36: `req`'s history, the assistant echo (≤ 4 000 chars) and a
    user turn listing ≤ 20 errors (messages cut to 200 chars) and the schema; temperature 0."""
    lines = "\n".join(
        f"{path}: {msg[:ERROR_MSG_MAX_CHARS]}" for path, msg in errors[:ERROR_LINES_MAX]
    )
    text = _REPAIR_INTRO + lines + _REPAIR_OUTRO + canonical_json(req.response_schema)
    echo = Message(role="assistant", parts=[TextPart(text=resp.text[:ECHO_MAX_CHARS])])
    ask = Message(role="user", parts=[TextPart(text=text)])
    return req.model_copy(update={"messages": [*req.messages, echo, ask], "temperature": 0.0})


def _record_repair(
    name: str, req: LLMRequest, repair_no: int, errors: _Errors, tracer: TracerLike | None
) -> None:
    """U08-35 step 8: the `repair` event (paths only, TH08-15) and the repairs counter."""
    detail = {
        "model_profile": name,
        "repair_no": repair_no,
        "error_paths": [path for path, _ in errors][:ERROR_LINES_MAX],
    }
    meta = req.metadata
    record_event(
        "repair",
        component="resilience",
        target=name,
        run_id=meta.run_id,
        task_id=meta.task_id,
        detail=detail,
        tracer=tracer,
    )
    record_counter(REPAIRS_METRIC, component="resilience", labels={"client": name})


async def complete_validated(
    client: AsyncCompleter,
    req: LLMRequest,
    *,
    max_repairs: int = 2,
    tracer: TracerLike | None = None,
    model: type[BaseModel] | None = None,
) -> LLMResponse:
    """Call ``client`` until its reply validates against ``req.response_schema`` (or
    ``model``), repairing at most ``max_repairs`` times at temperature 0 (U08-35).

    Raises ConfigError without a response schema or for ``max_repairs`` outside 0-5, and
    OutputValidationError when the reply is still invalid after the last repair.
    """
    if req.response_schema is None:
        msg = "complete_validated needs req.response_schema"
        raise ConfigError(msg)
    if not 0 <= max_repairs <= MAX_REPAIRS_LIMIT:
        msg = f"max_repairs must be 0-{MAX_REPAIRS_LIMIT}"
        raise ConfigError(msg)
    current, repairs = req, 0
    while True:
        resp: LLMResponse = await client.acomplete(current)  # the port types it as Any
        errors = _errors_of(resp, current, client.name, model)
        if not errors:
            return resp
        if repairs == max_repairs:
            msg = f"output invalid after {repairs} repairs"
            raise OutputValidationError(msg, client=client.name)
        repairs += 1
        current = build_repair_request(current, resp, errors)
        # synchronous SQLite writes leave the event loop (ENG §2.5), as retry events do
        await asyncio.to_thread(_record_repair, client.name, req, repairs, errors, tracer)


class _RetryingCompleter:
    """One chain candidate under its retry policy and breaker `model:<key>` (U08-38 step 2b).

    Each attempt runs the `llm.call` fault hook, then the inner client; retries follow U08-28
    (`_run`, async form) with the trace target `llm`.
    """

    def __init__(
        self, inner: AsyncCompleter, p: RetryPolicy, key: str, tracer: TracerLike | None
    ) -> None:
        self._inner, self._policy, self._key, self._tracer = inner, p, key, tracer

    @property
    def name(self) -> str:
        """The chain key of this candidate (the `model_profile` of its events)."""
        return self._key

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        """One retried completion of ``req``."""

        async def call() -> LLMResponse:
            fault_point("llm.call", model=self._key, role=req.metadata.role)
            response: LLMResponse = await self._inner.acomplete(req)
            return response

        return await self._arun(call)

    async def _arun(self, call: Callable[[], Awaitable[LLMResponse]]) -> LLMResponse:
        """`retry._run` for a coroutine, with this candidate's tracer and target `llm`."""
        key, start = "model:" + self._key, clock.monotonic()
        try:
            return await _ainvoke(call, key, "model")
        except Exception as exc:  # noqa: BLE001 - handed to tenacity as attempt 1
            first = exc
        wiring = _Retry(self._policy, key, "model", self._tracer, "llm", start)
        retrier = tenacity.AsyncRetrying(
            sleep=process_state().asleep, before_sleep=wiring.aemit, **wiring.kwargs()
        )
        return await retrier(_areplay(first, lambda: _ainvoke(call, key, "model")))


def _reason(err: HernessError) -> str | None:
    """The fallback reason of ``err`` (U08-38 step 2e), or None when it must propagate."""
    for cls, reason in _REASONS:
        if isinstance(err, cls):
            return reason
    return None


def _record_fallback(
    req: LLMRequest, from_key: str, to_key: str, reason: str, tracer: TracerLike | None
) -> None:
    """U08-38 step 2e: the `fallback` event, then the fallbacks counter."""
    detail = {"from_profile": from_key, "to_profile": to_key, "reason": reason}
    meta = req.metadata
    record_event(
        "fallback",
        component="resilience",
        target=from_key,
        run_id=meta.run_id,
        task_id=meta.task_id,
        detail=detail,
        tracer=tracer,
    )
    record_counter(FALLBACKS_METRIC, component="resilience", labels=detail)


class ModelChain:
    """Execute `chain_for(model_role, depth)` with retry, repair and fallback (U08-37, U08-38).

    The constructor does no I/O; the four fields never change. Many tasks may share one
    instance: the only shared mutable state is `process_state().auth_dropped`, under its lock.
    """

    __slots__ = ("_depth", "_gpu", "_model_role", "_registry")

    def __init__(
        self, model_role: str, *, registry: ChainRegistry, depth: str, gpu: GpuStateReader
    ) -> None:
        if not model_role:
            msg = "ModelChain model_role must not be empty"
            raise ConfigError(msg)
        if depth not in DEPTHS:
            msg = f"ModelChain depth must be one of {sorted(DEPTHS)}"
            raise ConfigError(msg)
        self._model_role, self._registry, self._depth, self._gpu = model_role, registry, depth, gpu

    @property
    def model_role(self) -> str:
        """The model role whose chain this executes."""
        return self._model_role

    @property
    def registry(self) -> ChainRegistry:
        """The chain registry (spec 05 `LLMRegistry`)."""
        return self._registry

    @property
    def depth(self) -> str:
        """`fast`, `standard` or `deep`."""
        return self._depth

    @property
    def gpu(self) -> GpuStateReader:
        """The GPU state reader."""
        return self._gpu

    def candidates(self) -> list[str]:
        """The chain's client keys still usable now, in order (U08-37).

        Drops off-network clients without egress (TH08-07), GPU clients whose class is not
        loaded (all of them while `swapping`) and open breakers whose probe is not yet due.
        Called directly it applies no per-run `auth_dropped` filter; `acomplete` does.
        """
        chain = self._registry.chain_for(self._model_role, self._depth)
        egress = get_config().security.egress.enabled
        loaded = self._gpu.loaded_class()
        return [key for key in chain if self._usable(key, egress=egress, loaded=loaded)]

    def _usable(self, key: str, *, egress: bool, loaded: str) -> bool:
        info = self._registry.config(key)
        if info.off_network and not egress:
            return False
        if info.gpu_class is not None and info.gpu_class != loaded:
            return False
        b = breaker("model:" + key)
        if b.state() != "open":
            return True
        due = b.retry_at()
        return due is None or clock.now() >= due  # a due probe stays for the retry guard

    async def acomplete(
        self,
        req: LLMRequest,
        *,
        schema: type[BaseModel] | None = None,
        client_for: Callable[[str], AsyncCompleter],
        tracer: TracerLike | None = None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        """Complete ``req`` on the first candidate that answers (U08-38).

        With ``schema`` (or a request schema) the reply is validated and repaired; the second
        element is the validated ``schema`` instance, else None. Raises ModelUnavailable when
        no candidate is left, the last fallback error when the chain is exhausted, and any
        other error (budget, query, tool input, rate limit, …) at once.
        """
        run_id, state = req.metadata.run_id, process_state()
        kept = await asyncio.to_thread(self.candidates)  # breaker reads may hit SQLite
        with state.lock:
            cands = [key for key in kept if (run_id, key) not in state.auth_dropped]
        last: HernessError = ModelUnavailable(f"no available model for role {self._model_role}")
        if not cands:
            raise last
        for i, key in enumerate(cands):
            try:
                return await self._attempt(key, i == 0, req, schema, client_for, tracer)
            except HernessError as err:
                reason = _reason(err)
                if reason is None:
                    raise
                last = err
                if reason == "auth":
                    with state.lock:
                        state.auth_dropped.add((run_id, key))
                if i + 1 < len(cands):
                    nxt = cands[i + 1]
                    await asyncio.to_thread(_record_fallback, req, key, nxt, reason, tracer)
        _log.error("resilience.chain.exhausted", model_role=self._model_role, tried=cands)
        raise last

    async def _attempt(
        self,
        key: str,
        first: bool,
        req: LLMRequest,
        schema: type[BaseModel] | None,
        client_for: Callable[[str], AsyncCompleter],
        tracer: TracerLike | None,
    ) -> tuple[LLMResponse, BaseModel | None]:
        """U08-38 steps 2a-2d on one candidate, always starting from the original ``req``."""
        info = self._registry.config(key)
        # only the first candidate keeps the caller's timeout, and only for its own client
        timeout = req.timeout_s if first and key == req.client else info.timeout_s
        req_k = req.model_copy(update={"client": key, "timeout_s": timeout})
        wrapped = _RetryingCompleter(client_for(key), policy_for_client(info), key, tracer)
        if schema is None and req.response_schema is None:
            return await wrapped.acomplete(req_k), None
        if req_k.response_schema is None and schema is not None:
            update = {
                "response_schema": schema.model_json_schema(),
                "response_schema_name": schema.__name__,
            }
            req_k = req_k.model_copy(update=update)
        max_repairs = get_config().resilience.resilience.fallback.max_repairs
        resp = await complete_validated(
            wrapped, req_k, max_repairs=max_repairs, tracer=tracer, model=schema
        )
        return resp, None if schema is None else schema.model_validate(_json_of(resp))
