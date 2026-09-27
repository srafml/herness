"""Hosted Jev backend through the spec 10 egress guard (impl 03 U03-55, U03-56; design 03 §3.3).

`JevHostedDecider` reuses the U03-53 request logic of `_JevHttpBackend`. Every client comes
from `herness.core.egress.get_guard().http_client(...)` (R-06; `client_factory`: tests), so
each request passes `EgressGuard.check` in `GuardedTransport` (profile gate, allowlist,
re-scan, egress line). `EgressBlocked` is a `FatalError`: never retried, it propagates at
once. The API key is a `SecretStr` sent only in the `Authorization` header (TH03-14).
Registration as `("decider", "jev")` is T03-16's.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from contextvars import ContextVar
from typing import Annotated, Final

import httpx2
from pydantic import SecretStr, TypeAdapter, ValidationError

from herness.core import egress
from herness.core.errors import ModelUnavailable, OutputValidationError
from herness.core.types import Answer, DecisionInput, DecisionOutput, PolicyName, Question
from herness.enrich.deciders.jev_wire import AdaptiveLimiter, load_wire_body
from herness.enrich.deciders.openjev import _JevHttpBackend, _listed_models, _Session
from herness.enrich.settings import JevSettings

__all__ = ["JevHostedDecider"]

_DECIDE_TIMEOUT_S: Final = 30.0
_HEALTH_TIMEOUT_S: Final = 5.0
# Spec 10 U10-51 step 2 admits payload class "none" only for `model_download`, so the
# health probe (an empty-bodied GET) is sent as `redacted_text` like `decide` (see report).
_HEALTH_PAYLOAD: Final = "redacted_text"
# The `decider_version` field itself validates a returned model: one pattern source.
_MODEL_ID: Final[TypeAdapter[str]] = TypeAdapter(
    Annotated[str, DecisionOutput.model_fields["decider_version"]]
)
# The `model` of the reply being parsed, set by `_check` and read by `_send` in one task.
_RETURNED: Final[ContextVar[str | None]] = ContextVar("jev_returned_model", default=None)


class _Answers(dict[str, Answer]):
    """Parsed answers plus the `model` the hosted service said produced them."""

    model: str | None = None


def _returned_model(raw: bytes) -> str | None:
    """The reply's `model` (None when absent); a malformed one is an invalid reply."""
    model = load_wire_body(raw).get("model")
    if model is None:
        return None
    msg = "model: not a model id"
    if not isinstance(model, str):
        raise OutputValidationError(msg)
    try:
        return _MODEL_ID.validate_python(model)
    except ValidationError as exc:
        raise OutputValidationError(msg) from exc


class JevHostedDecider(_JevHttpBackend):
    """Hosted Jev adapter (U03-55); `version` starts as `settings.model` and is fixed once
    by the first successful `health()` (U03-56). The constructor builds nothing; one guarded
    client per `decide` call, shared by its thread pool and closed at its end.
    """

    name = "jev"
    _policy: PolicyName = "decider_cloud"
    _breaker_key = "decider:jev"

    def __init__(
        self,
        settings: JevSettings,
        *,
        api_key: SecretStr,
        samples: int | None,
        client_factory: Callable[[], httpx2.Client] | None = None,
    ) -> None:
        super().__init__(
            model=settings.model,
            concurrency=settings.concurrency,
            api_key=api_key,
            samples=samples,
            client_factory=client_factory,
        )
        self._settings = settings
        self._base = settings.base_url.rstrip("/")
        self._path = f"{self._base}/v1/systemone"  # absolute: guarded clients have no base
        self.version = settings.model
        self._version_lock = threading.Lock()
        self._version_fixed = False

    def _open_client(self, timeout_s: float | None = None) -> httpx2.Client:
        timeout = _DECIDE_TIMEOUT_S if timeout_s is None else timeout_s
        return egress.get_guard().http_client(
            "bulk_classification", "redacted_text", timeout=timeout
        )

    def _check(self, resp: httpx2.Response, limiter: AdaptiveLimiter) -> bytes:
        raw = super()._check(resp, limiter)
        _RETURNED.set(_returned_model(raw))
        return raw

    async def _send(
        self, session: _Session, body: dict[str, object], asked: Sequence[Question]
    ) -> dict[str, Answer]:
        _RETURNED.set(None)
        answers = _Answers(await super()._send(session, body, asked))
        answers.model = _RETURNED.get()
        return answers

    def _output(
        self, item: DecisionInput, answers: dict[str, Answer], error: str | None
    ) -> DecisionOutput:
        out = super()._output(item, answers, error)
        model = getattr(answers, "model", None)  # U03-55 postcondition: the returned model
        if model is None or model == out.decider_version:
            return out
        return out.model_copy(update={"decider_version": model})

    def health(self) -> None:
        """Probe `GET /v1/models` through the guard (U03-56) and fix `version` once.

        A 200 listing fixes `version` to the listed id equal to `settings.model` (else
        `settings.model`). Any failure, `EgressBlocked` included, raises
        ModelUnavailable("jev health: <status or error class>").
        """
        try:
            factory = self._client_factory
            client = factory() if factory else self._health_client()
            try:
                resp = client.get(f"{self._base}/v1/models", headers=self._headers())
            finally:
                client.close()
            if resp.status_code == 200:  # noqa: PLR2004 - HTTP status
                _listed_models(resp.content)  # a malformed listing is a failure
                self._fix_version(self._settings.model)
                return
            reason = f"HTTP {resp.status_code}"
        except Exception as exc:
            msg = f"jev health: {type(exc).__name__}"
            raise ModelUnavailable(msg) from exc
        msg = f"jev health: {reason}"
        raise ModelUnavailable(msg)

    def _health_client(self) -> httpx2.Client:
        return egress.get_guard().http_client(
            "bulk_classification", _HEALTH_PAYLOAD, timeout=_HEALTH_TIMEOUT_S
        )

    def _fix_version(self, model: str) -> None:
        """`version` = the listed id equal to `model`, else `model` (both are `model`, so the
        listing is a liveness check; replies carry the served model); set at most once."""
        with self._version_lock:
            if not self._version_fixed:
                self.version = model
                self._version_fixed = True
