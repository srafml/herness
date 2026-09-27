"""Tests for herness.enrich.deciders.jev_hosted.JevHostedDecider (U03-55, U03-56, T03-13).

The allowing guard is the real `get_guard()` over a `premium` config whose egress admits
`bulk_classification` to `api.typesafe.ai`; `ScriptedNet` (a MockNet) answers below its
`GuardedTransport`, so the guard check, re-scan and egress line run for real and no socket
is opened. The refusing guard is a stub whose client transport raises `EgressBlocked`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx2
import pytest
from pydantic import SecretStr
from tests.support.egress_harness import egress_lines, fixed_redactor
from tests.unit.enrich._openjev_support import JSON, QS, Reply, install, item, jev_env, ok

from herness.core import _egress_transport as et
from herness.core import config as c
from herness.core import egress
from herness.core import redact as r
from herness.core.errors import ConfigError, EgressBlocked, ModelUnavailable
from herness.core.resilience import ProcessState
from herness.enrich.deciders.jev_hosted import JevHostedDecider
from herness.enrich.settings import JevSettings

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_KEY = SecretStr("unit-jev-token")
_URL = "https://api.typesafe.ai/v1/systemone"
_MODELS = "https://api.typesafe.ai/v1/models"
_PREMIUM = """\
version: 1
security:
  egress: {enabled: true, destinations: [api.typesafe.ai], purposes: [bulk_classification]}
"""


@pytest.fixture
def premium(
    jev_env: ProcessState, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> c.HernessConfig:
    """`jev_env` reloaded as a `premium` profile that admits hosted Jev; the guard's re-scan
    uses the fixed-key test redactor."""
    del jev_env
    cfg_dir = tmp_path / "config"  # the tree `jev_env` wrote
    (cfg_dir / "profiles" / "premium.yaml").write_text(_PREMIUM, encoding="utf-8")
    c.reset_config()
    cfg = c.init_config("premium", config_dir=cfg_dir, env={})
    monkeypatch.setattr(r._State, "redactor", fixed_redactor())
    return cfg


def _decider(samples: int | None = None, **kw: Any) -> JevHostedDecider:
    return JevHostedDecider(JevSettings(**kw), api_key=_KEY, samples=samples)


class _Spy:
    """Wrap the real guard's `http_client`, keeping each call's arguments and client."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        guard = egress.get_guard()
        real = guard.http_client
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any], httpx2.Client]] = []

        def spy(*a: Any, **k: Any) -> httpx2.Client:
            client = real(*a, **k)
            self.calls.append((a, k, client))
            return client

        monkeypatch.setattr(guard, "http_client", spy)


class _Refusing(httpx2.BaseTransport):
    """A guard stub's transport: every request is refused, and counted."""

    def __init__(self) -> None:
        self.count = 0

    def handle_request(self, request: httpx2.Request) -> httpx2.Response:
        self.count += 1
        msg = "egress blocked (egr_x): profile_forbids_egress"
        raise EgressBlocked(msg, reason="profile_forbids_egress")


class _StubGuard:
    def __init__(self) -> None:
        self.transport = _Refusing()
        self.calls: list[tuple[tuple[Any, ...], dict[str, Any]]] = []

    def http_client(self, *a: Any, **k: Any) -> httpx2.Client:
        self.calls.append((a, k))
        return httpx2.Client(transport=self.transport)


def _stub_guard(monkeypatch: pytest.MonkeyPatch) -> _StubGuard:
    stub = _StubGuard()
    monkeypatch.setattr(egress, "get_guard", lambda: stub)
    return stub


def _listing(*ids: str) -> Reply:
    return 200, json.dumps({"data": [{"id": i} for i in ids]}).encode(), JSON


# --- construction ----------------------------------------------------------------------------


def test_ut03_54_construction_builds_no_client(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-54 name jev, version settings.model, no guard or client touched at construction."""
    calls: list[object] = []
    monkeypatch.setattr(egress, "get_guard", lambda: calls.append(1))
    decider = _decider()
    assert (decider.name, decider.version) == ("jev", "jev-latest")
    assert calls == []


def test_ut03_54_samples_validated() -> None:
    """UT03-54 samples outside None/1/3/5 are refused (shared U03-53 rule)."""
    with pytest.raises(ConfigError, match="samples"):
        _decider(samples=2)


# --- decide through an allowing guard --------------------------------------------------------


def test_ut03_54_decide_through_allowing_guard(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 guard allowing: one guarded client http_client("bulk_classification",
    "redacted_text", timeout=30.0), absolute URLs, bearer per request, egress line per
    request, client closed, outputs in order with decider jev."""
    spy = _Spy(monkeypatch)
    net = install(monkeypatch, ok)
    items = [item(1), item(2), item(3)]
    out = _decider(samples=3).decide(items, QS)

    assert [o.record_id for o in out] == ["INC00001", "INC00002", "INC00003"]
    assert all(o.error is None and o.decider == "jev" for o in out)
    assert all(o.decider_version == "jev-latest" for o in out)
    assert all(set(o.answers) == {"is_outage", "severity", "urgency"} for o in out)
    assert [str(r.url) for r in net.requests] == [_URL] * 3
    assert all(r.headers["authorization"] == "Bearer unit-jev-token" for r in net.requests)
    assert all(b["model"] == "jev-latest" and b["samples"] == 3 for b in net.bodies())
    assert len(spy.calls) == 1
    args, kwargs, client = spy.calls[0]
    assert args == ("bulk_classification", "redacted_text")
    assert kwargs == {"timeout": 30.0}
    assert client.is_closed
    lines = [ln for ln in egress_lines(premium.paths.logs) if ln.get("decision") == "allowed"]
    assert len(lines) == 3
    assert {ln["destination"] for ln in lines} == {"api.typesafe.ai"}
    assert {ln["payload_class"] for ln in lines} == {"redacted_text"}


def test_ut03_54_returned_model_recorded_as_version(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 a reply whose model differs from version is recorded with that model; a
    reply without model keeps version (U03-55 postcondition)."""
    del premium

    def handler(body: dict[str, Any]) -> Reply:
        status, raw, headers = ok(body)
        payload = json.loads(raw)
        if body["state"] == item(1).text:
            payload["model"] = "jev-2026-09-01"
        else:
            del payload["model"]
        return status, json.dumps(payload).encode(), headers

    install(monkeypatch, handler)
    out = _decider().decide([item(1), item(2)], QS)
    assert [o.decider_version for o in out] == ["jev-2026-09-01", "jev-latest"]
    assert all(o.error is None for o in out)


def test_ut03_54_resend_without_model_drops_stale_returned_model(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 attempt 1 carries a model but invalid answers; the resend is valid and omits
    model -> the output records version, never the stale model of the rejected reply."""
    del premium
    replies: list[Reply] = []

    def handler(body: dict[str, Any]) -> Reply:
        status, raw, headers = ok(body)
        payload = json.loads(raw)
        if not replies:
            payload = {"model": "jev-stale", "answers": {}}
        else:
            del payload["model"]
        replies.append((status, json.dumps(payload).encode(), headers))
        return replies[-1]

    net = install(monkeypatch, handler)
    out = _decider().decide([item(1)], QS)
    assert len(net.requests) == 2
    assert out[0].error is None
    assert out[0].decider_version == "jev-latest"


@pytest.mark.parametrize("model", ["bad model!", 7, "x" * 129])
def test_ut03_54_malformed_returned_model_is_item_error(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch, model: object
) -> None:
    """UT03-54 a returned model that is not a model id is an invalid reply: sent once more,
    then DecisionOutput(error="OutputValidationError")."""
    del premium

    def handler(body: dict[str, Any]) -> Reply:
        status, raw, headers = ok(body)
        return status, json.dumps({**json.loads(raw), "model": model}).encode(), headers

    net = install(monkeypatch, handler)
    out = _decider().decide([item(1)], QS)
    assert out[0].error == "OutputValidationError"
    assert out[0].answers == {}
    assert out[0].decider_version == "jev-latest"
    assert len(net.requests) == 2


def test_ut03_54_response_too_large_propagates(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 the guard's response cap (EgressBlocked response_too_large) propagates once."""
    del premium
    monkeypatch.setattr(et, "MAX_RESPONSE_BYTES", 256)
    net = install(monkeypatch, lambda _b: (200, b"x" * 4096, JSON))
    with pytest.raises(EgressBlocked) as info:
        _decider().decide([item(1)], QS)
    assert info.value.reason == "response_too_large"
    assert len(net.requests) == 1


# --- decide through a refusing guard ---------------------------------------------------------


def test_ut03_54_egress_blocked_propagates_never_retried(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 guard stub raising EgressBlocked: decide raises it at once, one request,
    no retry (policy decider_cloud allows 3 attempts), client from the guard."""
    stub = _stub_guard(monkeypatch)
    with pytest.raises(EgressBlocked) as info:
        _decider().decide([item(1)], QS)
    assert info.value.reason == "profile_forbids_egress"
    assert stub.transport.count == 1
    assert stub.calls == [(("bulk_classification", "redacted_text"), {"timeout": 30.0})]


def test_ut03_54_egress_blocked_cancels_batch(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 with several items each is attempted at most once before the batch fails."""
    stub = _stub_guard(monkeypatch)
    with pytest.raises(EgressBlocked):
        _decider().decide([item(i) for i in range(1, 6)], QS)
    assert 1 <= stub.transport.count <= 5


# --- health ----------------------------------------------------------------------------------


def test_ut03_54_health_sets_version_from_listing(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 guard allowing: GET <base_url>/v1/models through a 5 s guarded client; the
    listed model fixes version; a later probe does not change it."""
    del premium
    spy = _Spy(monkeypatch)
    net = install(monkeypatch, lambda _b: _listing("jev-small", "jev-latest"))
    decider = _decider()
    decider.health()
    assert decider.version == "jev-latest"
    assert [(r.method, str(r.url)) for r in net.requests] == [("GET", _MODELS)]
    assert net.requests[0].headers["authorization"] == "Bearer unit-jev-token"
    args, kwargs, client = spy.calls[0]
    assert args == ("bulk_classification", "redacted_text")
    assert kwargs == {"timeout": 5.0}
    assert client.is_closed
    decider.version = "changed-elsewhere"
    decider.health()
    assert decider.version == "changed-elsewhere"  # fixed once per instance


def test_ut03_54_health_model_not_listed_keeps_settings_model(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 a 200 listing without settings.model leaves version = settings.model."""
    del premium
    install(monkeypatch, lambda _b: _listing("other"))
    decider = _decider()
    decider.health()
    assert decider.version == "jev-latest"


@pytest.mark.parametrize(
    ("reply", "reason"),
    [
        ((503, b"{}", JSON), "jev health: HTTP 503"),
        ((200, b"{not json", JSON), "jev health: OutputValidationError"),
    ],
)
def test_ut03_54_health_failures_are_model_unavailable(
    premium: c.HernessConfig, monkeypatch: pytest.MonkeyPatch, reply: Reply, reason: str
) -> None:
    """UT03-54 a non-200 status or a malformed listing raises ModelUnavailable."""
    del premium
    install(monkeypatch, lambda _b: reply)
    with pytest.raises(ModelUnavailable) as info:
        _decider().health()
    assert str(info.value) == reason


def test_ut03_54_health_egress_blocked_is_model_unavailable(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 guard stub raising EgressBlocked: health raises ModelUnavailable (cause kept)."""
    stub = _stub_guard(monkeypatch)
    with pytest.raises(ModelUnavailable, match=r"^jev health: EgressBlocked$") as info:
        _decider().health()
    assert isinstance(info.value.__cause__, EgressBlocked)
    assert stub.transport.count == 1


def test_ut03_54_health_uses_client_factory(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-54 client_factory (tests only) replaces the guard for decide and health."""
    guard_calls: list[object] = []
    monkeypatch.setattr(egress, "get_guard", lambda: guard_calls.append(1))
    seen: list[str] = []

    def handle(request: httpx2.Request) -> httpx2.Response:
        seen.append(str(request.url))
        if request.method == "GET":
            return httpx2.Response(200, json={"data": [{"id": "jev-latest"}]})
        body = json.loads(request.content)
        status, raw, headers = ok(body)
        return httpx2.Response(status, headers=headers, content=raw)

    decider = JevHostedDecider(
        JevSettings(base_url="https://api.typesafe.ai/"),
        api_key=_KEY,
        samples=None,
        client_factory=lambda: httpx2.Client(transport=httpx2.MockTransport(handle)),
    )
    decider.health()
    out = decider.decide([item(1)], QS)
    assert out[0].error is None
    assert seen == [_MODELS, _URL]
    assert guard_calls == []
