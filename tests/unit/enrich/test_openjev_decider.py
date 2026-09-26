"""Tests for herness.enrich.deciders.openjev.OpenJevDecider (U03-52 ... U03-54, T03-12).

The loopback client comes from the real `loopback_http_client`; `ScriptedNet` (a MockNet,
the httpx2 stand-in for respx) answers below its loopback-only transport. Payloads are
hand-built in the Jev shape (T03-11 precedent); `jev_env` binds a migrated ops store for
the `decider:openjev` breaker and loads the full config (policy `decider_local`).
"""

from __future__ import annotations

import json
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.unit.enrich._openjev_support import (
    JSON,
    QS,
    Reply,
    install,
    item,
    jev_env,
    ok,
)

from herness.core import egress
from herness.core.errors import (
    AuthError,
    ConfigError,
    EgressBlocked,
    FatalError,
    ModelUnavailable,
    RateLimited,
)
from herness.core.resilience import ProcessState, process_state
from herness.core.settings import OpenJevDeploy
from herness.enrich.deciders import openjev as oj
from herness.enrich.deciders.jev_wire import AdaptiveLimiter
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_IMAGE = "razorback16/openjev:0.4.0@sha256:" + "0" * 64
_KEY = SecretStr("unit-openjev-token")
_BASE = "http://127.0.0.1:8100"


def _tag(image: str) -> str:
    """The text between `:` and `@` of the pinned image (U03-52 preconditions)."""
    return image.split(":", 1)[1].split("@", 1)[0]


def _decider(
    samples: int | None = None, api_key: SecretStr | None = _KEY, **kw: Any
) -> OpenJevDecider:
    settings = OpenJevSettings(**kw)
    return OpenJevDecider(settings, api_key=api_key, image_tag="0.4.0", samples=samples)


def _status(code: int, headers: dict[str, str] | None = None) -> Reply:
    return code, b'{"detail": "nope"}', {**JSON, **(headers or {})}


def test_ut03_50_version_from_pinned_image() -> None:
    """UT03-50 image razorback16/openjev:0.4.0@sha256:... gives openjev-0.4.0/openjev-latest."""
    pin = OpenJevDeploy(image=_IMAGE, revision="r1")
    tag = _tag(pin.image)
    decider = OpenJevDecider(OpenJevSettings(), api_key=None, image_tag=tag, samples=None)
    assert decider.name == "openjev"
    assert decider.version == "openjev-0.4.0/openjev-latest"


@pytest.mark.parametrize("samples", [0, 2, 4, 7])
def test_ut03_50_samples_must_be_1_3_or_5(samples: int) -> None:
    """UT03-50 samples outside None/1/3/5 are refused at construction."""
    with pytest.raises(ConfigError, match="samples"):
        _decider(samples=samples)


def test_ut03_50_construction_builds_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-50 the constructor builds nothing (U03-52 side effects: none)."""
    calls: list[object] = []
    monkeypatch.setattr(egress, "loopback_http_client", lambda *a, **k: calls.append(a))
    _decider()
    assert calls == []


@pytest.mark.parametrize("samples", [None, 5])
def test_ut03_51_three_items_in_order(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, samples: int | None
) -> None:
    """UT03-51 3 outputs in order; samples omitted/sent; steps 1, think 0; one client from
    loopback_http_client(base_url, timeout_s=30); bearer sent per request; client closed."""
    real = egress.loopback_http_client
    made: list[tuple[tuple[object, ...], dict[str, object], httpx2.Client]] = []

    def spy(*a: Any, **k: Any) -> httpx2.Client:
        client = real(*a, **k)
        made.append((a, k, client))
        return client

    monkeypatch.setattr(egress, "loopback_http_client", spy)
    net = install(monkeypatch, ok)
    items = [item(1), item(2), item(3)]
    out = _decider(samples=samples).decide(items, QS)

    assert [o.record_id for o in out] == ["INC00001", "INC00002", "INC00003"]
    assert all(o.error is None and o.decider == "openjev" for o in out)
    assert all(set(o.answers) == {"is_outage", "severity", "urgency"} for o in out)
    assert out[0].decider_version == "openjev-0.4.0/openjev-latest"
    assert out[0].answers["severity"].answer == "low"
    assert out[0].answers["urgency"].answer == "1"
    bodies = net.bodies()
    assert sorted(b["state"] for b in bodies) == [i.text for i in items]
    for body in bodies:
        assert body["steps"] == 1
        assert body["think"] == 0
        assert body["model"] == "openjev-latest"
        assert list(body["questions"]) == ["is_outage", "severity", "urgency"]
        assert ("samples" in body) is (samples is not None)
        assert body.get("samples") == samples
    assert all(req.url.path == "/v1/systemone" for req in net.requests)
    assert all(req.headers["authorization"] == "Bearer unit-openjev-token" for req in net.requests)
    assert len(made) == 1
    args, kwargs, client = made[0]
    assert args == (_BASE,)
    assert kwargs["timeout_s"] == 30.0
    assert kwargs["bearer"] is not None
    assert client.is_closed


def test_ut03_51_no_key_sends_no_authorization(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-51 without an API key no Authorization header is sent."""
    net = install(monkeypatch, ok)
    out = _decider(api_key=None).decide([item(1)], QS)
    assert out[0].error is None
    assert "authorization" not in net.requests[0].headers


def test_ut03_51_client_factory_and_question_ids(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-51 client_factory is used when given (bearer still per request); question_ids
    select the asked questions; an item with none asked sends nothing."""
    net = install(monkeypatch, ok)
    clients: list[httpx2.Client] = []

    def factory() -> httpx2.Client:
        clients.append(egress.loopback_http_client(_BASE, timeout_s=30))
        return clients[-1]

    decider = OpenJevDecider(
        OpenJevSettings(), api_key=_KEY, image_tag="0.4.0", samples=1, client_factory=factory
    )
    out = decider.decide([item(1, ("severity",)), item(2, ())], QS)
    assert set(out[0].answers) == {"severity"}
    assert out[1].answers == {}
    assert out[1].error is None
    assert len(net.requests) == 1
    assert net.requests[0].headers["authorization"] == "Bearer unit-openjev-token"
    assert net.bodies()[0]["samples"] == 1
    assert len(clients) == 1
    assert clients[0].is_closed


def test_ut03_51_latency_and_error_metrics(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-51 latency histogram per request and errors counter by class are recorded."""
    seen: dict[str, int] = {}

    def handler(body: dict[str, Any]) -> Reply:
        seen[body["state"]] = seen.get(body["state"], 0) + 1
        if body["state"] == item(2).text:
            return 200, b"{not json", JSON
        return ok(body)

    install(monkeypatch, handler)
    _decider().decide([item(1), item(2)], QS)
    buffer = process_state().metric_buffer
    latencies = [k for k, _ in buffer.histograms if k[0] == oj.LATENCY_METRIC]
    assert len(latencies) == 3
    assert latencies[0][1] == (("decider", "openjev"),)
    errors = {k: v for k, v in buffer.counters.items() if k[0] == oj.ERRORS_METRIC}
    key = (oj.ERRORS_METRIC, (("decider", "openjev"), ("error_class", "OutputValidationError")))
    assert errors == {(*key, "enrich"): 2.0}


def test_ut03_52_malformed_item_retried_once_then_error(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 item 2 malformed twice: error output after 2 requests; others succeed."""
    seen: dict[str, int] = {}

    def handler(body: dict[str, Any]) -> Reply:
        seen[body["state"]] = seen.get(body["state"], 0) + 1
        if body["state"] == item(2).text:
            return 200, b"{not json", JSON
        return ok(body)

    install(monkeypatch, handler)
    with capture_logs() as logs:
        out = _decider().decide([item(1), item(2), item(3)], QS)
    assert seen[item(2).text] == 2
    assert seen[item(1).text] == seen[item(3).text] == 1
    assert out[1].error == "OutputValidationError"
    assert out[1].answers == {}
    assert out[0].error is None
    assert out[2].error is None
    failed = [e for e in logs if e["event"] == "enrich.decide.item_failed"]
    assert failed == [
        {
            "event": "enrich.decide.item_failed",
            "log_level": "warning",
            "component": "enrich.decider",
            "decider": "openjev",
            "error_class": "OutputValidationError",
        }
    ]


def test_ut03_52_malformed_once_then_ok(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 one malformed reply then a valid one: the item succeeds on its retry."""
    calls: list[int] = []

    def handler(body: dict[str, Any]) -> Reply:
        calls.append(1)
        return (200, b"[]", JSON) if len(calls) == 1 else ok(body)

    install(monkeypatch, handler)
    out = _decider().decide([item(1)], QS)
    assert len(calls) == 2
    assert out[0].error is None


@pytest.mark.parametrize(
    "reply",
    [
        _status(400),
        _status(422),
        (200, json.dumps({"answers": {}}).encode(), JSON),
        (200, json.dumps({"model": "x"}).encode(), JSON),
        (200, json.dumps({"answers": {"is_outage": {"noul": 2}}}).encode(), JSON),
    ],
    ids=["400", "422", "missing-answers", "no-answers", "out-of-range"],
)
def test_ut03_52_invalid_replies_become_item_errors(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, reply: Reply
) -> None:
    """UT03-52 400/422, missing answers and bad values: 2 requests, then an item error."""
    net = install(monkeypatch, lambda _body: reply)
    out = _decider().decide([item(1)], QS)
    assert len(net.requests) == 2
    assert out[0].error == "OutputValidationError"


@pytest.mark.parametrize("code", [401, 403])
def test_ut03_52_auth_error_propagates(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """UT03-52 401/403 -> AuthError, not retried, propagated out of decide."""
    net = install(monkeypatch, lambda _body: _status(code))
    with pytest.raises(AuthError, match=f"openjev: HTTP {code}"):
        _decider().decide([item(1)], QS)
    assert len(net.requests) == 1


def test_ut03_52_auth_error_cancels_the_other_items(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 a backend-level failure on one item cancels the gather and propagates."""

    def handler(body: dict[str, Any]) -> Reply:
        return _status(401) if body["state"] == item(2).text else ok(body)

    install(monkeypatch, handler)
    with pytest.raises(AuthError):
        _decider(concurrency=1).decide([item(1), item(2), item(3), item(4)], QS)


@pytest.mark.parametrize("code", [529, 500, 503])
def test_ut03_52_unavailable_after_policy_retries(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, code: int
) -> None:
    """UT03-52 529 x3 -> ModelUnavailable after the 3 attempts of `decider_local`."""
    net = install(monkeypatch, lambda _body: _status(code))
    with pytest.raises(ModelUnavailable, match=f"HTTP {code}"):
        _decider().decide([item(1)], QS)
    assert len(net.requests) == 3


def test_ut03_52_other_status_classified(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 a status outside the mapped ones goes through classify (404 -> ConfigError)."""
    net = install(monkeypatch, lambda _body: _status(404))
    with pytest.raises(ConfigError, match="HTTP 404"):
        _decider().decide([item(1)], QS)
    assert len(net.requests) == 1


def test_ut03_52_transport_errors_are_model_unavailable(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 connect errors (httpx2) map to ModelUnavailable and are retried."""
    calls: list[int] = []

    def refuse(body: dict[str, Any]) -> Reply:
        calls.append(1)
        msg = "refused"
        raise httpx2.ConnectError(msg)

    install(monkeypatch, refuse)
    with pytest.raises(ModelUnavailable, match="ConnectError"):
        _decider().decide([item(1)], QS)
    assert len(calls) == 3


def test_ut03_52_unknown_transport_error_is_fatal(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 other transport errors go through classify (unclassified -> FatalError)."""

    def broken(body: dict[str, Any]) -> Reply:
        msg = "odd"
        raise httpx2.UnsupportedProtocol(msg)

    install(monkeypatch, broken)
    with pytest.raises(FatalError):
        _decider().decide([item(1)], QS)


def test_ut03_52_rate_limited_then_ok(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 429 with Retry-After within the cap: limiter halved, retried, then ok."""
    calls: list[int] = []
    halved: list[float | None] = []
    real = AdaptiveLimiter.on_rate_limited

    def spy(self: AdaptiveLimiter, retry_after: float | None) -> None:
        halved.append(retry_after)
        real(self, retry_after)

    monkeypatch.setattr(AdaptiveLimiter, "on_rate_limited", spy)

    def handler(body: dict[str, Any]) -> Reply:
        calls.append(1)
        return _status(429, {"retry-after": "0"}) if len(calls) == 1 else ok(body)

    install(monkeypatch, handler)
    out = _decider().decide([item(1)], QS)
    assert out[0].error is None
    assert halved == [0.0]
    assert len(calls) == 2


def test_ut03_52_rate_limited_over_cap_propagates(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 429 whose Retry-After exceeds the policy cap (30 s) raises RateLimited."""
    net = install(monkeypatch, lambda _body: _status(429, {"retry-after": "120"}))
    with pytest.raises(RateLimited) as info:
        _decider().decide([item(1)], QS)
    assert info.value.retry_after == 120.0
    assert len(net.requests) == 1


def test_ut03_52_non_loopback_base_url_is_blocked(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-52 a base_url that bypassed the validator -> EgressBlocked propagates."""
    net = install(monkeypatch, ok)
    settings = OpenJevSettings().model_copy(update={"base_url": "http://10.0.0.7:8100"})
    decider = OpenJevDecider(settings, api_key=_KEY, image_tag="0.4.0", samples=None)
    with pytest.raises(EgressBlocked):
        decider.decide([item(1)], QS)
    assert net.requests == []


def _models(*ids: str) -> Reply:
    return 200, json.dumps({"data": [{"id": i} for i in ids]}).encode(), JSON


def test_ut03_53_health_ok_when_model_listed(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-53 /v1/models 200 listing the model -> ok (client from timeout_s=5, closed)."""
    real = egress.loopback_http_client
    made: list[tuple[dict[str, object], httpx2.Client]] = []

    def spy(*a: Any, **k: Any) -> httpx2.Client:
        made.append((k, real(*a, **k)))
        return made[-1][1]

    monkeypatch.setattr(egress, "loopback_http_client", spy)
    net = install(monkeypatch, lambda _body: _models("other", "openjev-latest"))
    _decider().health()
    assert net.requests[0].method == "GET"
    assert net.requests[0].url.path == "/v1/models"
    assert net.requests[0].headers["authorization"] == "Bearer unit-openjev-token"
    assert made[0][0]["timeout_s"] == 5.0
    assert made[0][1].is_closed


@pytest.mark.parametrize(
    ("reply", "why"),
    [
        (_models("other"), "model not listed"),
        (_status(500), "HTTP 500"),
        ((200, b"{nope", JSON), "OutputValidationError"),
        ((200, json.dumps({"data": {"id": "x"}}).encode(), JSON), "model not listed"),
        ((200, b" " * 70_000, JSON), "OutputValidationError"),
    ],
    ids=["unlisted", "500", "bad-json", "not-a-list", "over-64kb"],
)
def test_ut03_53_health_failures(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, reply: Reply, why: str
) -> None:
    """UT03-53 model missing, 500 and unreadable listings -> ModelUnavailable."""
    install(monkeypatch, lambda _body: reply)
    with pytest.raises(ModelUnavailable, match=rf"^openjev health: {why}$"):
        _decider().health()


def test_ut03_53_health_transport_error_and_factory(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-53 a transport error -> ModelUnavailable naming its class; factory client closed."""

    def refuse(body: dict[str, Any]) -> Reply:
        msg = "refused"
        raise httpx2.ConnectError(msg)

    install(monkeypatch, refuse)
    clients: list[httpx2.Client] = []

    def factory() -> httpx2.Client:
        clients.append(egress.loopback_http_client(_BASE, timeout_s=5))
        return clients[-1]

    decider = OpenJevDecider(
        OpenJevSettings(), api_key=None, image_tag="0.4.0", samples=None, client_factory=factory
    )
    with pytest.raises(ModelUnavailable, match=r"^openjev health: ConnectError$"):
        decider.health()
    assert clients[0].is_closed
