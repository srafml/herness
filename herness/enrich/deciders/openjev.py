"""OpenJev backend over loopback HTTP (impl 03 U03-52 ... U03-54, design 03 §3.3).

`_JevHttpBackend` holds the U03-53 request logic, shared with hosted Jev (U03-55). Clients
come only from `herness.core.egress.loopback_http_client` (R-06; `client_factory`: tests).
The API key is a `SecretStr` sent only in the `Authorization` header, never logged or put
in an error (TH03-14). Registration is T03-16's. One `decide` at a time per instance.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import functools
from collections.abc import Callable, Mapping, Sequence
from typing import Final, NamedTuple

import httpx  # types and exceptions only: the status mirror handed to `classify` (T08-04b)
import httpx2
from pydantic import SecretStr

from herness.core import egress
from herness.core.errors import (
    AuthError,
    ConfigError,
    HernessError,
    ModelUnavailable,
    OutputValidationError,
    RateLimited,
)
from herness.core.logging import get_logger
from herness.core.resilience.classify import classify
from herness.core.resilience.faults import fault_point
from herness.core.resilience.metrics import record_counter, timed
from herness.core.resilience.retry import aretry_call
from herness.core.types import (
    Answer,
    DecisionInput,
    DecisionOutput,
    PolicyName,
    Question,
    QuestionSet,
)
from herness.enrich.deciders.jev_wire import (
    AdaptiveLimiter,
    load_wire_body,
    parse_wire_answers,
    to_wire_questions,
)
from herness.enrich.questions import PAIR_QUESTIONS
from herness.enrich.settings import OpenJevSettings

__all__ = ["ERRORS_METRIC", "LATENCY_METRIC", "OpenJevDecider"]

LATENCY_METRIC: Final = "herness_enrich_decider_latency_seconds"
ERRORS_METRIC: Final = "herness_enrich_decider_errors_total"
_SAMPLES: Final = frozenset({1, 3, 5})
_HEALTH_TIMEOUT_S: Final = 5.0
_HEALTH_MAX_BYTES: Final = 65_536
_DOWN: Final = (httpx2.ConnectError, httpx2.TimeoutException, httpx2.RemoteProtocolError)
_RETRY_HEADERS: Final = frozenset({"retry-after", "x-ratelimit-reset"})

_log = get_logger("enrich.decider")


def _asked(item: DecisionInput, questions: QuestionSet) -> tuple[Question, ...]:
    """Questions asked for `item` (impl 03 §3.9 shared rules)."""
    if item.question_ids is not None:
        return tuple(questions.get(qid) for qid in item.question_ids)
    subset = questions.for_entity(item.entity).questions
    return tuple(q for q in subset if q.id not in PAIR_QUESTIONS)


def _parse(raw: bytes, asked: Sequence[Question]) -> dict[str, Answer]:
    """Strictly parse one response body: every asked question answered (TH03-06)."""
    answers = load_wire_body(raw).get("answers")
    parsed = parse_wire_answers(answers, asked) if isinstance(answers, Mapping) else {}
    if len(parsed) != len(asked):  # `asked` is never empty here
        msg = "answers: missing or not an object"
        raise OutputValidationError(msg)
    return parsed


def _classified(resp: httpx2.Response) -> HernessError:
    """T08-04b: `classify` knows only `httpx`, so the status (and Retry-After) is mirrored
    into an `httpx.HTTPStatusError`, never the body or request headers (TH03-14)."""
    request = httpx.Request(resp.request.method, str(resp.request.url.copy_with(query=None)))
    headers = {k: v for k, v in resp.headers.items() if k.lower() in _RETRY_HEADERS}
    mirror = httpx.Response(resp.status_code, headers=headers, request=request)
    return classify(
        httpx.HTTPStatusError("status", request=request, response=mirror), family="decider"
    )


def _transport_error(exc: Exception) -> HernessError:
    """T08-04b: `classify` rule 3 for `httpx2` transport errors (connect, timeout, protocol)."""
    if isinstance(exc, _DOWN):
        msg = f"decider call failed: {type(exc).__name__}"
        return ModelUnavailable(msg)
    return classify(exc, family="decider")


class _Session(NamedTuple):  # what one `decide` call shares between its items
    client: httpx2.Client
    pool: concurrent.futures.ThreadPoolExecutor
    limiter: AdaptiveLimiter
    loop: asyncio.AbstractEventLoop


class _JevHttpBackend:
    """Jev-shape HTTP request logic (U03-53) shared by OpenJev and hosted Jev (U03-55)."""

    name: str
    version: str
    _policy: PolicyName
    _breaker_key: str
    _path: str

    def __init__(
        self,
        *,
        model: str,
        concurrency: int,
        api_key: SecretStr | None,
        samples: int | None,
        client_factory: Callable[[], httpx2.Client] | None,
    ) -> None:
        if samples is not None and samples not in _SAMPLES:
            msg = "samples must be None, 1, 3 or 5"
            raise ConfigError(msg)
        self._model = model
        self._concurrency = concurrency
        self._api_key = api_key
        self._samples = samples
        self._client_factory = client_factory

    def _open_client(self, timeout_s: float | None = None) -> httpx2.Client:
        raise NotImplementedError  # pragma: no cover - every backend overrides it

    def _headers(self) -> dict[str, str]:
        if self._api_key is None:
            return {}
        return {"Authorization": f"Bearer {self._api_key.get_secret_value()}"}

    def decide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        """One request per record with all its questions (U03-53); outputs in input order.

        Raises AuthError, ModelUnavailable, CircuitOpen, RateLimited (Retry-After over the
        policy cap) and EgressBlocked; item-level failures become error outputs.
        """
        return asyncio.run(self._adecide(items, questions))

    async def _adecide(
        self, items: Sequence[DecisionInput], questions: QuestionSet
    ) -> list[DecisionOutput]:
        asked = [_asked(item, questions) for item in items]
        client = self._client_factory() if self._client_factory else self._open_client()
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=self._concurrency)
        session = _Session(
            client, pool, AdaptiveLimiter(self._concurrency), asyncio.get_running_loop()
        )
        try:
            async with asyncio.TaskGroup() as group:
                tasks = [
                    group.create_task(self._one(session, item, qs))
                    for item, qs in zip(items, asked, strict=True)
                ]
        except BaseExceptionGroup as failed:
            first = failed.exceptions[0]  # the gather is cancelled; the first failure wins
            raise first from first.__cause__
        finally:
            pool.shutdown(wait=True)
            client.close()
        return [task.result() for task in tasks]

    async def _one(
        self, session: _Session, item: DecisionInput, asked: tuple[Question, ...]
    ) -> DecisionOutput:
        if not asked:
            return self._output(item, {}, None)
        body: dict[str, object] = {
            "model": self._model,
            "state": item.text,
            "questions": to_wire_questions(asked),
            "steps": 1,
            "think": 0,
        }
        if self._samples is not None:
            body["samples"] = self._samples
        send = functools.partial(self._send, session, body, asked)
        error = "OutputValidationError"
        for _attempt in range(2):  # an invalid reply is sent once more (§6)
            try:
                async with session.limiter.slot():
                    answers = await aretry_call(self._policy, send, breaker_key=self._breaker_key)
                return self._output(item, answers, None)
            except OutputValidationError as exc:
                error = type(exc).__name__
        _log.warning("enrich.decide.item_failed", decider=self.name, error_class=error)
        return self._output(item, {}, error)

    async def _send(
        self, session: _Session, body: dict[str, object], asked: Sequence[Question]
    ) -> dict[str, Answer]:
        """One request: fault point, blocking post in the pool, status mapping, parse."""
        try:
            fault_point("decider.batch")
            post = functools.partial(
                session.client.post, self._path, json=body, headers=self._headers()
            )
            try:
                with timed(LATENCY_METRIC, component="enrich", labels={"decider": self.name}):
                    resp = await session.loop.run_in_executor(session.pool, post)
            except httpx2.TransportError as exc:
                raise _transport_error(exc) from exc
            return _parse(self._check(resp, session.limiter), asked)
        except HernessError as err:
            labels = {"decider": self.name, "error_class": type(err).__name__}
            record_counter(ERRORS_METRIC, component="enrich", labels=labels)
            raise

    def _check(self, resp: httpx2.Response, limiter: AdaptiveLimiter) -> bytes:
        status = resp.status_code
        if status == 200:  # noqa: PLR2004 - HTTP status
            return resp.content
        msg = f"{self.name}: HTTP {status}"
        if status in {400, 422}:
            raise OutputValidationError(msg)
        if status in {401, 403}:
            raise AuthError(msg)
        err = _classified(resp)
        if isinstance(err, RateLimited):
            limiter.on_rate_limited(err.retry_after)
        raise err

    def _output(
        self, item: DecisionInput, answers: dict[str, Answer], error: str | None
    ) -> DecisionOutput:
        return DecisionOutput(
            record_id=item.record_id,
            content_hash=item.content_hash,
            decider=self.name,  # type: ignore[arg-type]  # "openjev" or "jev"
            decider_version=self.version,
            answers=answers,
            error=error,
        )


def _listed_models(body: bytes) -> set[str]:
    """The `data[*].id` values of a `/v1/models` listing (≤ 64 KB)."""
    if len(body) > _HEALTH_MAX_BYTES:
        msg = "body: listing exceeds 64 KB"
        raise OutputValidationError(msg)
    data = load_wire_body(body).get("data")
    entries = data if isinstance(data, list) else []
    return {e["id"] for e in entries if isinstance(e, dict) and isinstance(e.get("id"), str)}


class OpenJevDecider(_JevHttpBackend):
    """OpenJev adapter over loopback HTTP (U03-52).

    `version = f"openjev-{image_tag}/{settings.model}"`; `image_tag` is the text between
    `:` and `@` of the pinned `deploy.openjev.image`. `samples` is None (omitted), 1, 3 or 5.
    The constructor builds nothing; one client per `decide` call, closed at its end.
    """

    name = "openjev"
    _policy: PolicyName = "decider_local"
    _breaker_key = "decider:openjev"
    _path = "/v1/systemone"

    def __init__(
        self,
        settings: OpenJevSettings,
        *,
        api_key: SecretStr | None,
        image_tag: str,
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
        self.version = f"openjev-{image_tag}/{settings.model}"

    def _open_client(self, timeout_s: float | None = None) -> httpx2.Client:
        timeout_s = self._settings.timeout_s if timeout_s is None else timeout_s
        return egress.loopback_http_client(
            self._settings.base_url, timeout_s=timeout_s, bearer=self._api_key
        )

    def health(self) -> None:
        """Probe `GET /v1/models` (U03-54): returns only when it lists `settings.model`.

        Raises ModelUnavailable("openjev health: <status or error class>") on any failure.
        """
        try:
            factory = self._client_factory
            client = factory() if factory else self._open_client(_HEALTH_TIMEOUT_S)
            try:
                resp = client.get("/v1/models", headers=self._headers())
            finally:
                client.close()
            if resp.status_code != 200:  # noqa: PLR2004 - HTTP status
                reason = f"HTTP {resp.status_code}"
            elif self._settings.model not in _listed_models(resp.content):
                reason = "model not listed"
            else:
                return
        except Exception as exc:
            msg = f"openjev health: {type(exc).__name__}"
            raise ModelUnavailable(msg) from exc
        msg = f"openjev health: {reason}"
        raise ModelUnavailable(msg)
