"""Tests for herness.enrich.deciders.llm (U03-60 ... U03-64, T03-15).

Votes run through the real `aretry_call("llm_local", complete_validated, ...)` over the
`jev_env` resilience environment (ops store backend, full test config); the model is the
test-local `FakeLLMClient` (impl 11 U11-42 carry-over), scripted per vote by `seed`.
"""

from __future__ import annotations

import asyncio
import datetime
import math
import re
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st
from structlog.testing import capture_logs
from tests.unit.enrich._fake_llm import FakeLLMClient, Reply, votes_by_seed
from tests.unit.enrich._openjev_support import BOOL, CHOICE, QS, SCORE, item, jev_env

from herness.core.errors import (
    AuthError,
    CircuitOpen,
    ConfigError,
    EgressBlocked,
    HernessError,
    ModelUnavailable,
    OutputValidationError,
)
from herness.core.resilience import ProcessState, aretry_call
from herness.core.types import DecisionInput, LLMRequest, LLMResponse
from herness.enrich.deciders import llm
from herness.enrich.deciders.llm import (
    CompletionClient,
    LlmDecider,
    vote_distribution,
    vote_schema,
)

pytestmark = pytest.mark.unit

__all__ = ["jev_env"]  # the fixture is used by name

_PROMPTS = Path(llm.__file__).resolve().parents[1] / "prompts"
_OPEN = '<untrusted_data source="enrich.text_redacted" record_id="INC00001">'
_GOOD = {"is_outage": {"answer": "true"}, "severity": {"answer": "high"}}
_OTHER = {"is_outage": {"answer": "false"}, "severity": {"answer": "high"}}
_BAD = {"is_outage": {"answer": "maybe"}, "severity": {"answer": "high"}}
_ASKED = ("is_outage", "severity")


def _decider(client: CompletionClient, votes: int = 3, **kw: object) -> LlmDecider:
    args: dict[str, object] = {"temperature": 0.7, "max_concurrency": 2, **kw}
    return LlmDecider(client, version="local/qwen-test", votes=votes, **args)  # type: ignore[arg-type]


def _one(decider: LlmDecider, i: int = 1) -> list[object]:
    return list(decider.decide([item(i, _ASKED)], QS))


def _user_text(req: LLMRequest) -> str:
    part = req.messages[0].parts[0]
    assert part.type == "text"
    return part.text


# --- UT03-58 -------------------------------------------------------------------------------


def test_ut03_58_fake_client_is_a_completion_client() -> None:
    """UT03-58 the scripted fake satisfies the runtime-checkable CompletionClient."""
    assert isinstance(FakeLLMClient(votes_by_seed({})), CompletionClient)
    assert not isinstance(object(), CompletionClient)


# --- UT03-59 -------------------------------------------------------------------------------


def test_ut03_59_vote_schema_mixed_questions() -> None:
    """UT03-59 enums per question type and `additionalProperties: false` everywhere."""

    def one(labels: list[str]) -> dict[str, object]:
        return {
            "type": "object",
            "properties": {"answer": {"enum": labels}},
            "required": ["answer"],
            "additionalProperties": False,
        }

    assert vote_schema([BOOL, CHOICE, SCORE]) == {
        "type": "object",
        "properties": {
            "is_outage": one(["true", "false"]),
            "severity": one(["low", "mid", "high"]),
            "urgency": one(["0", "1", "2", "3"]),
        },
        "required": ["is_outage", "severity", "urgency"],
        "additionalProperties": False,
    }


# --- UT03-60 / PT03-06 -----------------------------------------------------------------------


def test_ut03_60_laplace_smoothed_distribution() -> None:
    """UT03-60 votes [a, a, b] over K = 3 give 2.5/4.5, 1.5/4.5, 0.5/4.5."""
    dist = vote_distribution(["a", "a", "b"], ["a", "b", "c"])
    assert list(dist) == ["a", "b", "c"]
    assert dist["a"] == pytest.approx(2.5 / 4.5)
    assert dist["b"] == pytest.approx(1.5 / 4.5)
    assert dist["c"] == pytest.approx(0.5 / 4.5)


def test_ut03_60_vote_outside_labels_rejected() -> None:
    """UT03-60 a vote that is not a label raises OutputValidationError."""
    with pytest.raises(OutputValidationError, match="vote outside labels"):
        vote_distribution(["a", "z"], ["a", "b"])


@given(
    st.lists(st.text(min_size=1, max_size=5), min_size=2, max_size=12, unique=True).flatmap(
        lambda labels: st.tuples(
            st.just(labels), st.lists(st.sampled_from(labels), min_size=1, max_size=25)
        )
    )
)
def test_pt03_06_distribution_sums_to_one_all_positive(
    case: tuple[list[str], list[str]],
) -> None:
    """PT03-06 the distribution sums to 1 and every label gets a probability > 0."""
    labels, votes = case
    dist = vote_distribution(votes, labels)
    assert set(dist) == set(labels)
    assert math.isclose(sum(dist.values()), 1.0, abs_tol=1e-9)
    assert all(p > 0 for p in dist.values())


# --- UT03-61 -------------------------------------------------------------------------------


def test_ut03_61_three_votes_one_invalid_after_repairs(jev_env: ProcessState) -> None:
    """UT03-61 an invalid vote (1 call + 2 repairs) is dropped; 2 votes make the distribution."""
    client = FakeLLMClient(votes_by_seed({0: _GOOD, 1: _BAD, 2: _GOOD}))
    decider = _decider(client)
    [out] = decider.decide([item(1, _ASKED)], QS)
    assert out.error is None
    assert out.decider == "llm"
    assert out.decider_version == "local/qwen-test"
    assert (out.record_id, out.content_hash) == ("INC00001", f"{1:032x}")
    outage, severity = out.answers["is_outage"], out.answers["severity"]
    assert outage.answer == "true"
    assert outage.distribution == pytest.approx({"true": 2.5 / 3, "false": 0.5 / 3})
    assert outage.probability == pytest.approx(2.5 / 3)
    assert outage.backend_confidence is None
    assert severity.distribution == pytest.approx(
        {"low": 0.5 / 3.5, "mid": 0.5 / 3.5, "high": 2.5 / 3.5}
    )
    assert len(client.requests) == 5  # votes 0 and 2 once, vote 1 three times
    assert decider.samples == 3


def test_ut03_61_request_fields(jev_env: ProcessState) -> None:
    """UT03-61 each vote request follows U03-63 step 2 (seed, schema, metadata, header)."""
    client = FakeLLMClient(votes_by_seed(dict.fromkeys(range(5), _GOOD)))
    _one(_decider(client, votes=5, temperature=0.4))
    header = llm.load_prompt(_PROMPTS / "enrich_decider.md")[0]
    for i, req in enumerate(client.requests):
        assert req.client == "local-decider"
        assert [b.text for b in req.system] == [header]
        assert req.response_schema == vote_schema([BOOL, CHOICE])
        assert req.response_schema_name == "enrich_votes"
        assert (req.temperature, req.seed, req.thinking) == (0.4, i, "off")
        meta = req.metadata
        assert (meta.task_id, meta.role, meta.model_role) == (
            None,
            "enrich_decider",
            "enrich_decider",
        )
        assert (meta.step, meta.request_key) == (i, f"{1:032x}:{i}")


def test_ut03_61_user_message_paraphrase_questions_and_block(jev_env: ProcessState) -> None:
    """UT03-61 vote i uses paraphrase i mod 5, lists the questions, then the wrapped text."""
    client = FakeLLMClient(votes_by_seed(dict.fromkeys(range(5), _GOOD)))
    decider = _decider(client, votes=5)
    decider.decide([item(1, ("is_outage", "severity", "urgency"))], QS)
    paraphrases = llm.load_prompt(_PROMPTS / "enrich_decider.md")[1]
    texts = [_user_text(r) for r in client.first_requests()]
    assert len(set(texts)) == 5
    for i, text in enumerate(texts):
        assert text.startswith(paraphrases[i % 5])
        assert "- id: severity; type: choice; instructions: Classify the ticket text." in text
        assert "  - high: High impact" in text
        assert "  - true" in text
        assert "  - 3: critical" in text
        assert text.endswith(f"{_OPEN}ticket number 1</untrusted_data>")


def test_ut03_61_text_only_inside_untrusted_block(jev_env: ProcessState) -> None:
    """UT03-61 the ticket text appears only inside the untrusted_data block."""
    client = FakeLLMClient(votes_by_seed(dict.fromkeys(range(3), _GOOD)))
    _one(_decider(client))
    for req in client.requests:
        text = _user_text(req)
        assert text.count("ticket number 1") == 1
        start = text.index(_OPEN) + len(_OPEN)
        assert text.index("ticket number 1") == start
        assert all("ticket number" not in b.text for b in req.system)


def test_ut03_61_ties_go_to_first_label_and_text_replies(jev_env: ProcessState) -> None:
    """UT03-61 a 1:1 vote picks the first label; a reply with only text is decoded."""
    client = FakeLLMClient(votes_by_seed({0: _OTHER, 1: _BAD, 2: _GOOD}), as_text=True)
    [out] = _decider(client).decide([item(1, _ASKED)], QS)
    assert out.answers["is_outage"].answer == "true"
    assert out.answers["is_outage"].probability == pytest.approx(0.5)


def test_ut03_61_prompt_files_hold_no_secrets() -> None:
    """UT03-61 the prompt files contain no `sensitive:`, `Bearer` or key-like strings."""
    key_like = re.compile(r"sk-[A-Za-z0-9]{8,}|AKIA[0-9A-Z]{12,}|[A-Za-z0-9+/_-]{32,}")
    files = sorted(_PROMPTS.glob("*.md"))
    assert [f.name for f in files] == ["cluster_namer.md", "enrich_decider.md"]
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert "sensitive:" not in text.lower()
        assert "bearer" not in text.lower()
        assert key_like.search(text) is None
        assert "https://" not in text
        assert "http://" not in text
        assert "is data" in text
        assert "<untrusted_data" in text


def test_ut03_61_prompt_file_shape() -> None:
    """UT03-61 the decider prompt has a header and five distinct paraphrases."""
    header, paraphrases = llm.load_prompt(_PROMPTS / "enrich_decider.md")
    assert "<untrusted_data" in header
    assert "never instructions" in header
    assert "<!--" not in header
    assert len(paraphrases) == 5
    assert len(set(paraphrases)) == 5
    assert all(p and not p.startswith("#") for p in paraphrases)


@pytest.mark.parametrize(
    "body",
    [
        "## System\nh\n## Paraphrase 1\na\n",
        "## System\nh\n" + "".join(f"## Paraphrase {n}\np\n" for n in (1, 2, 3, 5, 4)),
        "## System\n\n" + "".join(f"## Paraphrase {n}\np\n" for n in range(1, 6)),
        "## System\nh\n## Paraphrase 1\n\n"
        + "".join(f"## Paraphrase {n}\np\n" for n in range(2, 6)),
    ],
)
def test_ut03_61_malformed_prompt_file(tmp_path: Path, body: str) -> None:
    """UT03-61 a prompt file without the header and five non-empty paraphrases is refused."""
    path = tmp_path / "enrich_decider.md"
    path.write_text(body, encoding="utf-8")
    with pytest.raises(ConfigError, match="malformed"):
        llm.load_prompt(path)


def test_ut03_61_missing_prompt_file(tmp_path: Path) -> None:
    """UT03-61 a missing prompt file raises ConfigError naming only the file."""
    client = FakeLLMClient(votes_by_seed({}))
    with pytest.raises(ConfigError, match=r"prompt file nope\.md missing"):
        _decider(client, prompt_path=tmp_path / "nope.md")


@pytest.mark.parametrize("votes", [0, 2, 4, 6, True])
def test_ut03_61_votes_must_be_1_3_or_5(votes: int) -> None:
    """UT03-61 votes outside {1, 3, 5} raise ConfigError."""
    with pytest.raises(ConfigError, match="votes"):
        _decider(FakeLLMClient(votes_by_seed({})), votes=votes)


@pytest.mark.parametrize("kw", [{"max_concurrency": 0}, {"version": "bad version"}])
def test_ut03_61_bad_constructor_arguments(kw: dict[str, object]) -> None:
    """UT03-61 max_concurrency < 1 or a version off the decider_version pattern is refused."""
    client = FakeLLMClient(votes_by_seed({}))
    args: dict[str, object] = {"version": "local/qwen-test", "max_concurrency": 1, **kw}
    with pytest.raises(ConfigError):
        LlmDecider(client, votes=1, temperature=0.7, **args)  # type: ignore[arg-type]


def test_ut03_61_outputs_in_input_order_and_default_questions(jev_env: ProcessState) -> None:
    """UT03-61 one output per input, in order; no question_ids → the entity's questions
    minus pair questions; an item with no questions asked gets empty answers and no call."""
    client = FakeLLMClient(votes_by_seed({0: {**_GOOD, "urgency": {"answer": "1"}}}))
    items = [item(3), item(4, ()), item(5)]
    outs = _decider(client, votes=1).decide(items, QS)
    assert [o.record_id for o in outs] == ["INC00003", "INC00004", "INC00005"]
    assert set(outs[0].answers) == {"is_outage", "severity", "urgency"}
    assert outs[1].answers == {}
    assert outs[1].error is None
    assert len(client.requests) == 2
    assert _decider(client).decide([], QS) == []


def test_ut03_61_record_id_cannot_break_the_attribute(jev_env: ProcessState) -> None:
    """UT03-61 quotes and angle brackets in a record id are escaped in the attribute."""
    client = FakeLLMClient(votes_by_seed({0: _GOOD}))
    odd = DecisionInput(
        record_id='X" source="evil"><b', entity="incident",
        content_hash=f"{9:032x}", text="plain", question_ids=_ASKED,
    )  # fmt: skip
    _decider(client, votes=1).decide([odd], QS)
    text = _user_text(client.requests[0])
    assert 'record_id="X&quot; source=&quot;evil&quot;&gt;&lt;b">plain</untrusted_data>' in text
    assert text.count('source="') == 1


_RETRY_AT = datetime.datetime(2030, 1, 1, tzinfo=datetime.UTC)


@pytest.mark.parametrize(
    "failure",
    [
        AuthError("denied"),
        ModelUnavailable("down"),
        CircuitOpen("open", key="decider:llm", retry_at=_RETRY_AT),
        EgressBlocked("blocked"),
    ],
    ids=["auth", "model_unavailable", "circuit_open", "egress_blocked"],
)
def test_ut03_61_backend_errors_propagate(jev_env: ProcessState, failure: HernessError) -> None:
    """UT03-61 a backend error from the client is not a dropped vote or an item error: it
    propagates out of decide as its own class. Items run one at a time: with two in flight,
    the retried ModelUnavailable calls of both can open the breaker first (CircuitOpen)."""
    client = FakeLLMClient(lambda _req: failure)
    with capture_logs() as logs, pytest.raises(type(failure)):
        _decider(client, max_concurrency=1).decide([item(1, _ASKED), item(2, _ASKED)], QS)
    assert not [e for e in logs if e["event"] == "enrich.decide.vote_dropped"]


class _SlowClient(FakeLLMClient):
    """Yields to the loop inside each call and records the peak number of calls in flight."""

    def __init__(self) -> None:
        super().__init__(votes_by_seed(dict.fromkeys(range(3), _GOOD)))
        self.in_flight = 0
        self.peak = 0

    async def acomplete(self, req: LLMRequest) -> LLMResponse:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await asyncio.sleep(0.01)
            return await super().acomplete(req)
        finally:
            self.in_flight -= 1


@pytest.mark.parametrize("max_concurrency", [1, 2, 3])
def test_ut03_61_calls_in_flight_bounded_by_max_concurrency(
    jev_env: ProcessState, max_concurrency: int
) -> None:
    """UT03-61 across several items at most `max_concurrency` model calls are in flight, and
    the bound is reached (items do run concurrently when it allows)."""
    client = _SlowClient()
    items = [item(i, _ASKED) for i in range(1, 8)]
    outs = _decider(client, votes=3, max_concurrency=max_concurrency).decide(items, QS)
    assert [o.error for o in outs] == [None] * 7
    assert len(client.requests) == 21
    assert client.peak == max_concurrency


def test_ut03_61_votes_run_under_the_decider_breaker(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT03-61 every vote runs through aretry_call("llm_local", ...) with
    breaker_key="decider:llm"."""
    seen: list[tuple[object, object]] = []
    real = aretry_call

    async def spy(name: str, fn: object, /, *a: object, **kw: object) -> object:
        seen.append((name, kw.get("breaker_key")))
        return await real(name, fn, *a, **kw)  # type: ignore[arg-type]

    monkeypatch.setattr(llm, "aretry_call", spy)
    _one(_decider(FakeLLMClient(votes_by_seed(dict.fromkeys(range(3), _GOOD)))))
    assert seen == [("llm_local", "decider:llm")] * 3


# --- UT03-62 -------------------------------------------------------------------------------


def test_ut03_62_all_votes_invalid_is_an_item_error(jev_env: ProcessState) -> None:
    """UT03-62 zero valid votes give DecisionOutput(error="OutputValidationError")."""
    client = FakeLLMClient(votes_by_seed(dict.fromkeys(range(3), _BAD)))
    [out, ok] = _decider(client).decide([item(1, _ASKED), item(2, ("is_outage",))], QS)
    assert out.error == "OutputValidationError"
    assert out.answers == {}
    assert ok.error == "OutputValidationError"  # `_BAD` fails every schema that asks is_outage
    assert len(client.requests) == 18


def test_ut03_62_dropped_vote_and_item_error_logs_carry_no_text(jev_env: ProcessState) -> None:
    """UT03-62 the vote_dropped and item_failed events carry ids, counts and error classes
    only: never the ticket text."""
    sensitive = "customer Jane Roe cannot log in to payroll"

    def script(req: LLMRequest) -> Reply:
        assert req.seed is not None
        return _BAD if sensitive in _user_text(req) or req.seed == 1 else _GOOD

    client = FakeLLMClient(script)
    texts = [
        DecisionInput(
            record_id=f"INC0000{n}", entity="incident", content_hash=f"{n:032x}",
            text=body, question_ids=_ASKED,
        )
        for n, body in ((1, "plain outage note"), (2, sensitive))
    ]  # fmt: skip
    with capture_logs() as logs:
        first, second = _decider(client).decide(texts, QS)
    assert first.error is None
    assert second.error == "OutputValidationError"
    dropped = [e for e in logs if e["event"] == "enrich.decide.vote_dropped"]
    failed = [e for e in logs if e["event"] == "enrich.decide.item_failed"]
    assert len(dropped) == 4  # vote 1 of item 1, votes 0-2 of item 2
    assert len(failed) == 1
    for event in logs:
        rendered = repr(event)
        assert sensitive not in rendered
        assert "plain outage note" not in rendered
        assert "Jane" not in rendered


def test_ut03_62_health_success_is_a_one_token_probe() -> None:
    """UT03-62 health sends one 1-token completion at temperature 0 and returns."""
    client = FakeLLMClient(lambda _req: "ok")
    _decider(client).health()
    [req] = client.requests
    assert (req.max_output_tokens, req.temperature) == (1, 0.0)
    assert req.response_schema is None


@pytest.mark.parametrize("failure", [ModelUnavailable("down"), RuntimeError("boom")])
def test_ut03_62_health_failure_is_model_unavailable(failure: Reply) -> None:
    """UT03-62 any failure of the probe raises ModelUnavailable("llm health")."""
    client = FakeLLMClient(lambda _req: failure)
    with pytest.raises(ModelUnavailable, match="llm health"):
        _decider(client).health()


def test_ut03_62_health_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-62 a probe that outlives the 30 s bound raises ModelUnavailable("llm health")."""
    seen: list[float] = []

    def fake_timeout(fn: object, timeout_s: float, **_kw: object) -> object:
        seen.append(timeout_s)
        msg = "call timed out"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(llm, "call_with_timeout", fake_timeout)
    with pytest.raises(ModelUnavailable, match="llm health"):
        _decider(FakeLLMClient(lambda _req: "ok")).health()
    assert seen == [30.0]
