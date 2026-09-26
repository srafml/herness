"""Tests for herness.core.resilience.chain: complete_validated, build_repair_request and
ModelChain (impl 08 U08-35..U08-38; T08-09)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import pytest
import structlog
from pydantic import BaseModel
from tests.support.ops_store import OpsStoreHandle

from herness.core import config as c
from herness.core import redact as r
from herness.core import resilience
from herness.core import time as clock
from herness.core.errors import (
    AuthError,
    BudgetExceeded,
    CircuitOpen,
    ConfigError,
    EgressBlocked,
    ModelRefused,
    ModelUnavailable,
    OutputValidationError,
    QueryError,
    RateLimited,
    ToolInputError,
)
from herness.core.ids import canonical_json
from herness.core.resilience import ModelChain, ProcessState, complete_validated, process_state
from herness.core.resilience import chain as chain_module
from herness.core.resilience.chain import build_repair_request
from herness.core.types import LLMRequest, LLMResponse, Message, RequestMeta, TextPart, Usage
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

RETRY_FIELDS = {
    "target",
    "attempt",
    "error_type",
    "wait_s",
    "policy",
    "breaker_key",
    "retry_after_s",
}
REPAIR_FIELDS = {"model_profile", "repair_no", "error_paths"}
FALLBACK_FIELDS = {"from_profile", "to_profile", "reason"}
CHAIN = ["claude-opus", "local-30b", "local-large-offload", "local-small-cpu"]
SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"n": {"type": "integer"}, "a/b": {"type": "string"}},
    "required": ["n"],
}
VALID = '{"n": 1}'
MALFORMED = "not json {"


class Answer(BaseModel):
    n: int


@pytest.fixture
def env(
    ops_db: OpsStoreHandle, herness_cfg: c.HernessConfig, test_redactor: r.Redactor
) -> ProcessState:
    """A migrated ops store bound as the backend, the full test config (egress off)."""
    del ops_db, herness_cfg, test_redactor
    return process_state()


def _req(*, schema: dict[str, Any] | None = None, run_id: str = "run_A", client: str = "caller"):
    return LLMRequest(
        client=client,
        messages=[Message(role="user", parts=[TextPart(text="question")])],
        response_schema=schema,
        response_schema_name=None if schema is None else "Answer",
        max_output_tokens=100,
        timeout_s=7.0,
        metadata=RequestMeta(
            run_id=run_id,
            task_id="task_1",
            role="analyst",
            model_role="reasoning",
            step=0,
            request_key=f"{run_id}:0:x",
        ),
    )


def _resp(text: str, client: str, parsed: dict[str, Any] | None = None) -> LLMResponse:
    return LLMResponse(
        text=text,
        tool_calls=[],
        parsed=parsed,
        reasoning=[],
        stop_reason="end_turn",
        raw_stop_reason="stop",
        refusal_category=None,
        usage=Usage(),
        cost_usd=Decimal(0),
        client=client,
        model="m",
        provider="openai_compat",
        latency_ms=1,
        request_id=None,
    )


class Scripted:
    """An `AsyncCompleter` replaying texts or raising errors; the last item repeats."""

    def __init__(self, name: str, *script: str | BaseException) -> None:
        self.name = name
        self.script = list(script)
        self.calls: list[LLMRequest] = []

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.calls.append(req)
        item = self.script.pop(0) if len(self.script) > 1 else self.script[0]
        if isinstance(item, BaseException):
            raise item
        return _resp(item, self.name)


def _clients(**scripts: Scripted) -> Callable[[str], Scripted]:
    return lambda key: scripts[key]


def _chain(registry, gpu, chain: list[str] | None = None) -> ModelChain:
    registry.chains[("reasoning", "standard")] = list(CHAIN if chain is None else chain)
    return ModelChain("reasoning", registry=registry, depth="standard", gpu=gpu)


def _events(kind: str) -> list[dict[str, Any]]:
    rows = read_all("SELECT * FROM resilience_event WHERE kind = ? ORDER BY ts", (kind,))
    return [dict(row) | {"detail": json.loads(row["detail"])} for row in rows]


def _counter(name: str, **labels: str) -> float:
    key = (name, tuple(sorted(labels.items())), "resilience")
    return process_state().metric_buffer.counters.get(key, 0.0)


# --- UT08-33 .. UT08-36: repair ------------------------------------------------------------


def test_ut08_33_two_repairs_then_valid(env: ProcessState, recording_tracer) -> None:
    """UT08-33 2 malformed replies then valid: 3 calls, 2 `repair` events with repair_no 1, 2."""
    client = Scripted("local-30b", MALFORMED, '{"n": "x"}', VALID)
    resp = asyncio.run(complete_validated(client, _req(schema=SCHEMA), tracer=recording_tracer))
    assert resp.text == VALID
    assert len(client.calls) == 3
    repairs = recording_tracer.of("repair")
    assert [set(f) for f in repairs] == [REPAIR_FIELDS, REPAIR_FIELDS]
    assert [f["repair_no"] for f in repairs] == [1, 2]
    assert repairs[0]["error_paths"] == ["/"]
    assert repairs[1]["error_paths"] == ["/n"]
    assert all(f["model_profile"] == "local-30b" for f in repairs)
    rows = _events("repair")
    assert [(row["target"], row["run_id"], row["task_id"]) for row in rows] == [
        ("local-30b", "run_A", "task_1")
    ] * 2
    assert _counter("herness_resilience_repairs_total", client="local-30b") == 2.0
    # each repair call extends the history of the call before it, at temperature 0
    assert [len(call.messages) for call in client.calls] == [1, 3, 5]
    assert client.calls[0].temperature is None
    assert {call.temperature for call in client.calls[1:]} == {0.0}


def test_ut08_34_invalid_after_max_repairs(env: ProcessState) -> None:
    """UT08-34 3 malformed replies: OutputValidationError after 3 calls."""
    client = Scripted("local-30b", MALFORMED)
    with pytest.raises(OutputValidationError, match="after 2 repairs") as info:
        asyncio.run(complete_validated(client, _req(schema=SCHEMA)))
    assert len(client.calls) == 3
    assert info.value.context["client"] == "local-30b"


def test_ut08_34_zero_repairs_and_parsed_reply(env: ProcessState) -> None:
    """UT08-34 max_repairs=0 fails after one call; a `parsed` reply is used over its text."""
    with pytest.raises(OutputValidationError):
        asyncio.run(
            complete_validated(Scripted("c", MALFORMED), _req(schema=SCHEMA), max_repairs=0)
        )

    class Parsed(Scripted):
        async def acomplete(self, req: LLMRequest) -> LLMResponse:
            self.calls.append(req)
            return _resp(MALFORMED, self.name, parsed={"n": 3})

    resp = asyncio.run(complete_validated(Parsed("c", ""), _req(schema=SCHEMA), max_repairs=0))
    assert resp.parsed == {"n": 3}


def test_ut08_34_preconditions(env: ProcessState) -> None:
    """UT08-34 no response_schema, or max_repairs outside 0-5, is a ConfigError."""
    with pytest.raises(ConfigError, match="response_schema"):
        asyncio.run(complete_validated(Scripted("c", VALID), _req()))
    for bad in (-1, 6):
        with pytest.raises(ConfigError, match="max_repairs"):
            asyncio.run(
                complete_validated(Scripted("c", VALID), _req(schema=SCHEMA), max_repairs=bad)
            )


def test_ut08_35_build_repair_request_bounds() -> None:
    """UT08-35 5 000-char reply, 25 errors: echo 4 000 chars, 20 error lines, canonical schema,
    temperature 0.0; each error message is cut to 200 chars."""
    req = _req(schema=SCHEMA)
    errors = [(f"/f{i}", "m" * 300) for i in range(25)]
    out = build_repair_request(req, _resp("x" * 5000, "c"), errors)
    assert out.temperature == 0.0
    assert out.messages[:1] == req.messages
    echo, ask = out.messages[1:]
    assert (echo.role, ask.role) == ("assistant", "user")
    assert echo.parts[0].text == "x" * 4000
    text = ask.parts[0].text
    head, rest = text.split("Errors:\n", 1)
    assert head == "Your previous reply was not valid. "
    lines, schema = rest.split("\nReply with only a JSON object that matches this JSON Schema:\n")
    assert lines.splitlines() == [f"/f{i}: {'m' * 200}" for i in range(20)]
    assert schema == canonical_json(SCHEMA)
    assert (req.temperature, len(req.messages)) == (None, 1)  # the input is not changed


def test_ut08_36_pydantic_errors_hold_no_value(env: ProcessState, recording_tracer) -> None:
    """UT08-36 a wrong-typed value "synthetic-secret-123" never reaches error lines or paths."""
    marker = "synthetic-secret-123"  # the R-67 synthetic value
    client = Scripted("c", json.dumps({"n": marker}), VALID)
    req = _req(schema=Answer.model_json_schema())
    asyncio.run(complete_validated(client, req, model=Answer, tracer=recording_tracer))
    repair_text = client.calls[1].messages[-1].parts[0].text
    lines = repair_text.split("Errors:\n")[1].split("\nReply with")[0]
    assert lines.startswith("/n: Input should be a valid integer")
    assert marker not in lines
    assert recording_tracer.of("repair")[0]["error_paths"] == ["/n"]
    assert marker not in json.dumps(recording_tracer.events)
    assert marker not in json.dumps([row["detail"] for row in _events("repair")])


def test_ut08_36_jsonschema_pointer_escaping(env: ProcessState) -> None:
    """UT08-36 jsonschema errors become RFC 6901 pointers with the validator name only."""
    client = Scripted("c", json.dumps({"n": 1, "a/b": 5}), VALID)
    asyncio.run(complete_validated(client, _req(schema=SCHEMA)))
    text = client.calls[1].messages[-1].parts[0].text
    assert "/a~1b: failed 'type' constraint" in text
    tilde = Scripted("c", json.dumps([1]), VALID)
    asyncio.run(complete_validated(tilde, _req(schema={"type": "object"})))
    assert "\n/: failed 'type' constraint\n" in tilde.calls[1].messages[-1].parts[0].text


def test_ut08_36_invalid_json_message_has_no_document(env: ProcessState) -> None:
    """UT08-36 a JSON decode error keeps the decoder message, not the document."""
    client = Scripted("c", "{ secretdoc", VALID)
    asyncio.run(complete_validated(client, _req(schema=SCHEMA)))
    text = client.calls[1].messages[-1].parts[0].text
    assert "/: invalid JSON: " in text
    assert "secretdoc" not in text.split("Errors:\n")[1]


def test_ut08_33_fault_malformed_json_counts_as_invalid(env: ProcessState, fault_env) -> None:
    """UT08-33 the `llm.output` malformed_json fault is one invalid reply ("fault: ...")."""
    fault_env([{"point": "llm.output", "action": "malformed_json", "nth": 1}])
    client = Scripted("c", VALID)
    asyncio.run(complete_validated(client, _req(schema=SCHEMA)))
    assert len(client.calls) == 2
    assert "/: fault: malformed_json" in client.calls[1].messages[-1].parts[0].text


def test_st08_15_injected_value_not_echoed(env: ProcessState, recording_tracer) -> None:
    """ST08-15 an output value "<script>ignore previous</script>" failing a type check is in
    neither the repair error lines nor the trace `error_paths`."""
    evil = "<script>ignore previous</script>"
    client = Scripted("c", json.dumps({"n": evil}), json.dumps({"n": evil}), VALID)
    for model in (Answer, None):
        client.calls.clear()
        client.script = [json.dumps({"n": evil}), VALID]
        req = _req(schema=SCHEMA)
        asyncio.run(complete_validated(client, req, model=model, tracer=recording_tracer))
        lines = client.calls[1].messages[-1].parts[0].text.split("Errors:\n")[1]
        assert evil not in lines.split("\nReply with")[0]
    assert all(evil not in json.dumps(f) for f in recording_tracer.of("repair"))


# --- UT08-37 / ST08-07: candidates ------------------------------------------------------------


def test_ut08_37_candidates_filters(env: ProcessState, fake_chain_registry, fake_gpu_state) -> None:
    """UT08-37 egress off, reasoning loaded, small-cpu breaker open with its probe not due."""
    chain = _chain(fake_chain_registry, fake_gpu_state)
    resilience.breaker("model:local-small-cpu").force_open(ModelUnavailable("down"))
    assert chain.candidates() == ["local-30b"]
    fake_gpu_state.loaded = "large"
    assert chain.candidates() == ["local-large-offload"]
    fake_gpu_state.loaded = "swapping"
    assert chain.candidates() == []
    SqliteResilienceBackend().health_reset(["model:local-small-cpu"], clock.now())
    process_state().breakers.clear()
    assert chain.candidates() == ["local-small-cpu"]


def test_ut08_37_open_breaker_with_due_probe_stays(
    env: ProcessState, fake_chain_registry, fake_gpu_state, fake_clock
) -> None:
    """UT08-37 an open breaker whose probe is due stays in the list for the retry guard."""
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu"])
    resilience.breaker("model:local-small-cpu").force_open(ModelUnavailable("down"))
    assert chain.candidates() == []
    fake_clock.advance(3600)
    assert chain.candidates() == ["local-small-cpu"]


def test_ut08_37_egress_on_keeps_off_network(
    env: ProcessState, fake_chain_registry, fake_gpu_state, monkeypatch
) -> None:
    """UT08-37 with `security.egress.enabled` the off-network client is kept."""
    cfg = c.get_config()
    egress = cfg.security.egress.model_copy(update={"enabled": True})
    security = cfg.security.model_copy(update={"egress": egress})
    patched = cfg.model_copy(update={"security": security})
    monkeypatch.setattr(chain_module, "get_config", lambda: patched)
    chain = _chain(fake_chain_registry, fake_gpu_state)
    assert chain.candidates()[0] == "claude-opus"


def test_ut08_37_constructor_validation(fake_chain_registry, fake_gpu_state) -> None:
    """UT08-37 an empty model_role or an unknown depth is a ConfigError; fields are fixed."""
    with pytest.raises(ConfigError):
        ModelChain("", registry=fake_chain_registry, depth="fast", gpu=fake_gpu_state)
    with pytest.raises(ConfigError):
        ModelChain("r", registry=fake_chain_registry, depth="huge", gpu=fake_gpu_state)
    chain = ModelChain("r", registry=fake_chain_registry, depth="deep", gpu=fake_gpu_state)
    assert (chain.model_role, chain.depth) == ("r", "deep")
    assert (chain.registry, chain.gpu) == (fake_chain_registry, fake_gpu_state)


def test_st08_07_off_network_never_called_without_egress(
    env: ProcessState, fake_chain_registry, fake_gpu_state
) -> None:
    """ST08-07 profile `local` (egress off), chain starts with claude-opus: never called."""
    chain = _chain(fake_chain_registry, fake_gpu_state)
    called: list[str] = []

    def client_for(key: str) -> Scripted:
        called.append(key)
        return Scripted(key, VALID)

    resp, obj = asyncio.run(chain.acomplete(_req(), client_for=client_for))
    assert (called, resp.client, obj) == (["local-30b"], "local-30b", None)
    assert "claude-opus" not in chain.candidates()


# --- UT08-38 .. UT08-42: acomplete ------------------------------------------------------------


@pytest.mark.parametrize(
    ("error", "reason"),
    [(ModelRefused("no"), "refusal"), (EgressBlocked("blocked"), "egress_blocked")],
)
def test_ut08_38_refusal_and_egress_fall_back(
    env: ProcessState, fake_chain_registry, fake_gpu_state, recording_tracer, error, reason
) -> None:
    """UT08-38 candidate 1 raises ModelRefused / EgressBlocked: one call, answer from 2."""
    fake_chain_registry.clients["local-30b-b"] = fake_chain_registry.clients["local-30b"]
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-30b", "local-30b-b"])
    first, second = Scripted("local-30b", error), Scripted("local-30b-b", VALID)
    run = chain.acomplete(
        _req(),
        client_for=_clients(**{"local-30b": first, "local-30b-b": second}),
        tracer=recording_tracer,
    )
    resp, obj = asyncio.run(run)
    assert (len(first.calls), len(second.calls), resp.client, obj) == (1, 1, "local-30b-b", None)
    fallbacks = recording_tracer.of("fallback")
    assert fallbacks == [
        {"from_profile": "local-30b", "to_profile": "local-30b-b", "reason": reason}
    ]
    assert set(fallbacks[0]) == FALLBACK_FIELDS
    assert recording_tracer.of("retry") == []
    assert [row["detail"]["reason"] for row in _events("fallback")] == [reason]
    labels = {"from_profile": "local-30b", "to_profile": "local-30b-b", "reason": reason}
    assert _counter("herness_resilience_fallbacks_total", **labels) == 1.0
    assert second.calls[0].client == "local-30b-b"
    assert second.calls[0].timeout_s == 90.0  # the candidate's own timeout


def test_ut08_38_circuit_open_falls_back(
    env: ProcessState, fake_chain_registry, fake_gpu_state
) -> None:
    """UT08-38 a CircuitOpen from candidate 1 falls back with reason `circuit_open`."""
    fake_gpu_state.loaded = "none"
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu", "local-small-cpu-2"])
    fake_chain_registry.clients["local-small-cpu-2"] = fake_chain_registry.clients[
        "local-small-cpu"
    ]
    err = CircuitOpen("open", key="model:x", retry_at=clock.now())
    scripts = {"local-small-cpu": Scripted("a", err), "local-small-cpu-2": Scripted("b", VALID)}
    asyncio.run(chain.acomplete(_req(), client_for=_clients(**scripts)))
    assert [row["detail"]["reason"] for row in _events("fallback")] == ["circuit_open"]


def test_ut08_38_retry_event_traced_with_llm_target(
    env: ProcessState, fake_chain_registry, fake_gpu_state, recording_tracer
) -> None:
    """UT08-38 a ModelUnavailable is retried on the same candidate; the `retry` trace event
    has the exact U08-28 field set, target `llm` and breaker key `model:<key>`."""
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-30b"])
    client = Scripted("local-30b", ModelUnavailable("blip"), VALID)
    req = _req(client="local-30b")
    resp, _ = asyncio.run(
        chain.acomplete(req, client_for=_clients(**{"local-30b": client}), tracer=recording_tracer)
    )
    assert (resp.text, len(client.calls)) == (VALID, 2)
    assert client.calls[0].timeout_s == 7.0  # the caller's client keeps the caller's timeout
    retries = recording_tracer.of("retry")
    assert [set(f) for f in retries] == [RETRY_FIELDS]
    assert (retries[0]["target"], retries[0]["breaker_key"]) == ("llm", "model:local-30b")
    assert retries[0]["policy"] == "llm_local"
    assert recording_tracer.of("fallback") == []


def test_ut08_39_fallback_gets_original_messages(
    env: ProcessState, fake_chain_registry, fake_gpu_state, recording_tracer
) -> None:
    """UT08-39 candidate 1 always malformed: 3 calls on it, then candidate 2 receives the
    original messages (no repair history) and returns the validated model."""
    fake_gpu_state.loaded = "none"
    fake_chain_registry.clients["cpu-2"] = fake_chain_registry.clients["local-small-cpu"]
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu", "cpu-2"])
    first, second = Scripted("local-small-cpu", MALFORMED), Scripted("cpu-2", '{"n": 5}')
    req = _req()
    resp, obj = asyncio.run(
        chain.acomplete(
            req,
            schema=Answer,
            client_for=_clients(**{"local-small-cpu": first, "cpu-2": second}),
            tracer=recording_tracer,
        )
    )
    assert len(first.calls) == 3
    assert second.calls[0].messages == req.messages
    assert second.calls[0].temperature is None
    assert second.calls[0].response_schema == Answer.model_json_schema()
    assert second.calls[0].response_schema_name == "Answer"
    assert isinstance(obj, Answer)
    assert (obj.n, resp.client) == (5, "cpu-2")
    assert [f["repair_no"] for f in recording_tracer.of("repair")] == [1, 2]
    assert recording_tracer.of("fallback")[0]["reason"] == "validation"


def test_ut08_39_request_schema_without_model(
    env: ProcessState, fake_chain_registry, fake_gpu_state
) -> None:
    """UT08-39 a request with its own response_schema and no model is validated, obj None."""
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-30b"])
    client = Scripted("local-30b", '{"n": "x"}', VALID)
    resp, obj = asyncio.run(
        chain.acomplete(_req(schema=SCHEMA), client_for=_clients(**{"local-30b": client}))
    )
    assert (resp.text, obj, len(client.calls)) == (VALID, None, 2)


@pytest.mark.parametrize(
    "error",
    [
        BudgetExceeded("budget"),
        QueryError("sql"),
        ToolInputError("tool"),
        RateLimited("slow", retry_after=999),
    ],
)
def test_ut08_40_other_errors_propagate(
    env: ProcessState, fake_chain_registry, fake_gpu_state, error
) -> None:
    """UT08-40 BudgetExceeded / QueryError / ToolInputError (and RateLimited) raise at once."""
    fake_gpu_state.loaded = "none"
    fake_chain_registry.clients["cpu-2"] = fake_chain_registry.clients["local-small-cpu"]
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu", "cpu-2"])
    first, second = Scripted("a", error), Scripted("b", VALID)
    with pytest.raises(type(error)):
        asyncio.run(
            chain.acomplete(
                _req(), client_for=_clients(**{"local-small-cpu": first, "cpu-2": second})
            )
        )
    assert (len(first.calls), second.calls) == (1, [])
    assert _events("fallback") == []


def test_ut08_41_exhausted_and_empty(
    env: ProcessState, fake_chain_registry, fake_gpu_state
) -> None:
    """UT08-41 all candidates ModelUnavailable: the last error and `resilience.chain.exhausted`;
    an empty chain raises ModelUnavailable without calling anything."""
    fake_gpu_state.loaded = "none"
    fake_chain_registry.clients["cpu-2"] = fake_chain_registry.clients["local-small-cpu"]
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu", "cpu-2"])
    last = ModelUnavailable("second down")
    scripts = {
        "local-small-cpu": Scripted("a", ModelUnavailable("first down")),
        "cpu-2": Scripted("b", last),
    }
    with structlog.testing.capture_logs() as logs, pytest.raises(ModelUnavailable) as info:
        asyncio.run(chain.acomplete(_req(), client_for=_clients(**scripts)))
    assert info.value is last
    exhausted = [e for e in logs if e["event"] == "resilience.chain.exhausted"]
    assert exhausted[0]["log_level"] == "error"
    assert (exhausted[0]["model_role"], exhausted[0]["tried"]) == (
        "reasoning",
        ["local-small-cpu", "cpu-2"],
    )
    assert [row["detail"]["reason"] for row in _events("fallback")] == ["unavailable"]
    empty = _chain(fake_chain_registry, fake_gpu_state, [])
    with pytest.raises(ModelUnavailable, match="no available model for role reasoning"):
        asyncio.run(empty.acomplete(_req(), client_for=_clients()))


def test_ut08_42_auth_drops_candidate_per_run(
    env: ProcessState, fake_chain_registry, fake_gpu_state, recording_tracer
) -> None:
    """UT08-42 AuthError in run_A: fallback reason `auth`; the second run_A call skips
    candidate 1; run_B still tries it."""
    fake_gpu_state.loaded = "none"
    fake_chain_registry.clients["cpu-2"] = fake_chain_registry.clients["local-small-cpu"]
    chain = _chain(fake_chain_registry, fake_gpu_state, ["local-small-cpu", "cpu-2"])
    first, second = Scripted("a", AuthError("401")), Scripted("b", VALID)
    clients = _clients(**{"local-small-cpu": first, "cpu-2": second})
    for run_id in ("run_A", "run_A", "run_B"):
        asyncio.run(
            chain.acomplete(_req(run_id=run_id), client_for=clients, tracer=recording_tracer)
        )
    assert [call.metadata.run_id for call in first.calls] == ["run_A", "run_B"]
    assert len(second.calls) == 3
    assert [f["reason"] for f in recording_tracer.of("fallback")] == ["auth", "auth"]
    assert process_state().auth_dropped == {
        ("run_A", "local-small-cpu"),
        ("run_B", "local-small-cpu"),
    }
    assert chain.candidates() == ["local-small-cpu", "cpu-2"]  # no run filter when called directly
