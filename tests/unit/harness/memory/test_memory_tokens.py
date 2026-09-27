"""Tests for herness.harness.memory.tokens (impl 07 U07-68, U07-69; design 07 §5.2, §5.3)."""

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.core.errors import ConfigError
from herness.core.types import (
    LoopLimits,
    LoopState,
    Message,
    SystemBlock,
    TextPart,
    ToolSpec,
    Usage,
)
from herness.harness.llm import tokens as llm_tokens
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory import tokens as mt
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.tokens import TokenCounter, compute_thresholds

pytestmark = pytest.mark.unit

_PRICE = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}
_LIMITS = LoopLimits(context_budget_tokens=10_000, soft_tokens=5_000, hard_tokens=8_000)


def _client(tokenizer: str = "estimate", **over: Any) -> ClientConfig:
    fields: dict[str, Any] = {
        "name": "local-30b",
        "kind": "openai_compat",
        "base_url": "http://localhost:8000/v1",
        "model": "m",
        "context_window": 32_768,
        "max_output_tokens": 4_000,
        "tokenizer": tokenizer,
        "max_concurrency": 4,
        "price_per_mtok": _PRICE,
    }
    if tokenizer == "anthropic":
        fields |= {
            "name": "claude",
            "kind": "anthropic",
            "base_url": None,
            "thinking_mode": "adaptive_always",
            "price_per_mtok": {"input": "3", "output": "15", "cache_read": "0", "cache_write": "0"},
        }
    return ClientConfig.model_validate(fields | over)


def _msg(text: str, role: str = "user") -> Message:
    return Message(role=role, parts=[TextPart(text=text)])  # type: ignore[arg-type]


def _state(messages: list[Message]) -> LoopState:
    state = LoopState.fresh(messages[0], limits=_LIMITS, estimator=llm_tokens.estimate_tokens)
    state.messages = list(messages)
    return state


class FakeExact:
    """A fake spec 05 ``count_tokens``: 10 tokens per message, records every call."""

    def __init__(self, *, exact: bool = True) -> None:
        self.calls: list[int] = []
        self.exact = exact

    def __call__(
        self,
        cfg: ClientConfig,
        messages: list[Message],
        tools: list[ToolSpec],
        system: list[SystemBlock],
    ) -> tuple[int, bool]:
        assert tools == []
        assert system == []
        self.calls.append(len(messages))
        return 10 * len(messages), self.exact


# --- UT07-50 ---------------------------------------------------------------------------


def test_ut07_50_estimate_backend_count_state() -> None:
    """UT07-50 estimate backend: count_state equals est_input_tokens, with and without usage."""
    exact = FakeExact()
    counter = TokenCounter(_client(), exact=exact)
    msgs = [_msg("hello " * 40), _msg("a reply here", "assistant")]
    state = _state(msgs)
    assert counter.count_state(state, 1_000) == (state.est_input_tokens(), False)
    assert state.est_input_tokens() == llm_tokens.estimate_tokens(msgs)
    state.last_usage = Usage(input_tokens=5_000, cache_read_tokens=100)
    tokens, is_exact = counter.count_state(state, 1_000)
    assert (tokens, is_exact) == (state.est_input_tokens(), False)
    assert tokens == 5_100 + llm_tokens.estimate_tokens(msgs)
    assert exact.calls == []


def test_ut07_50_estimate_backend_count_messages() -> None:
    """UT07-50 estimate backend: count_messages equals spec 05 estimate_tokens, never exact."""
    exact = FakeExact()
    counter = TokenCounter(_client(), exact=exact)
    msgs = [_msg("x" * 700), _msg("y" * 7, "assistant")]
    assert counter.count_messages(msgs, 10) == (llm_tokens.estimate_tokens(msgs), False)
    assert counter.count_messages([], 10) == (0, False)
    assert exact.calls == []


def test_ut07_50_default_callables_are_spec_05() -> None:
    """UT07-50 the defaults are spec 05's count_tokens and estimate_tokens, not copies."""
    counter = TokenCounter(_client())
    assert counter._exact is llm_tokens.count_tokens
    assert counter._estimate is llm_tokens.estimate_tokens
    assert not hasattr(mt, "estimate_tokens")
    assert not hasattr(mt, "count_tokens")


def test_ut07_50_unknown_tokenizer_is_config_error() -> None:
    """UT07-50 a tokenizer outside the three backends is a ConfigError at construction."""
    cfg = ClientConfig.model_construct(name="odd", tokenizer="sentencepiece")  # type: ignore[call-arg, arg-type]
    with pytest.raises(ConfigError, match="unknown tokenizer for client odd"):
        TokenCounter(cfg)


# --- UT07-51 ---------------------------------------------------------------------------


def test_ut07_51_anthropic_exact_only_at_sixty_percent() -> None:
    """UT07-51 anthropic: exact count only when the estimate reaches 0.6 x budget."""
    exact = FakeExact()
    counter = TokenCounter(_client("anthropic"), exact=exact)
    msgs = [_msg("z" * 348)]
    assert llm_tokens.estimate_tokens(msgs) == 108  # 0.6 x 180
    assert counter.count_messages(msgs, 1_000) == (108, False)
    assert counter.count_messages(msgs, 181) == (108, False)  # 108 < 108.6
    assert exact.calls == []
    assert counter.count_messages(msgs, 180) == (10, True)  # 108 >= 108, inclusive
    assert counter.count_messages(msgs, 100) == (10, True)
    assert exact.calls == [1, 1]  # no cache for anthropic


def test_ut07_51_anthropic_count_state() -> None:
    """UT07-51 anthropic: count_state uses est_input_tokens, exact only above 0.6 x budget."""
    exact = FakeExact()
    counter = TokenCounter(_client("anthropic"), exact=exact)
    state = _state([_msg("z" * 348)])
    assert counter.count_state(state, 181) == (108, False)
    assert counter.count_state(state, 180) == (10, True)
    state.last_usage = Usage(input_tokens=1_000)
    assert counter.count_state(state, 5_000) == (1_108, False)
    assert counter.count_state(state, 1_800) == (10, True)
    assert exact.calls == [1, 1]


def test_ut07_51_vllm_per_message_cache() -> None:
    """UT07-51 vLLM: per-message exact counts are cached by content and summed."""
    exact = FakeExact()
    counter = TokenCounter(_client("vllm_endpoint"), exact=exact)
    a, b, c = _msg("a"), _msg("b", "assistant"), _msg("c")
    assert counter.count_messages([a, b], 10) == (20, True)
    assert exact.calls == [1, 1]
    assert counter.count_messages([a, b, c], 10) == (30, True)
    assert exact.calls == [1, 1, 1]  # a and b were cache hits
    assert counter.count_messages([_msg("a"), b], 10) == (20, True)  # equal content hits too
    assert exact.calls == [1, 1, 1]
    state = _state([a, b])
    assert counter.count_state(state, 10) == (20, True)  # the whole list, one call
    assert exact.calls == [1, 1, 1, 2]


def test_ut07_51_vllm_fallback_is_inexact_and_not_cached() -> None:
    """UT07-51 vLLM: a fallback count makes the sum inexact and is asked again next time."""
    exact = FakeExact(exact=False)
    counter = TokenCounter(_client("vllm_endpoint"), exact=exact)
    msgs = [_msg("a"), _msg("b")]
    assert counter.count_messages(msgs, 10) == (20, False)
    assert counter.count_messages(msgs, 10) == (20, False)
    assert exact.calls == [1, 1, 1, 1]
    exact.exact = True
    assert counter.count_messages(msgs, 10) == (20, True)


def test_ut07_51_vllm_cache_evicts_oldest(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT07-51 vLLM: the cache holds at most CACHE_MAX entries; the oldest is evicted."""
    assert mt.CACHE_MAX == 10_000
    monkeypatch.setattr(mt, "CACHE_MAX", 2)
    exact = FakeExact()
    counter = TokenCounter(_client("vllm_endpoint"), exact=exact)
    a, b, c = _msg("a"), _msg("b"), _msg("c")
    counter.count_messages([a, b, c], 10)
    assert len(counter._cache) == 2
    counter.count_messages([b, c], 10)
    assert exact.calls == [1, 1, 1]  # b and c still cached
    counter.count_messages([a], 10)
    assert exact.calls == [1, 1, 1, 1]  # a was evicted


# --- UT07-52 ---------------------------------------------------------------------------


def test_ut07_52_design_example() -> None:
    """UT07-52 32,768 / 4,000 gives 27,744 / 19,420 / 23,582 / 12,484."""
    stats = compute_thresholds(_client(), CompactionConfig(), 5, exact=True)
    assert (stats.budget, stats.soft, stats.hard, stats.target) == (27_744, 19_420, 23_582, 12_484)
    assert (stats.tokens, stats.exact) == (5, True)
    default = compute_thresholds(_client(), CompactionConfig())
    assert (default.tokens, default.exact) == (0, False)


def test_ut07_52_effective_context_and_safety() -> None:
    """UT07-52 max_effective_context caps the window; safety is 3 % above 1,024."""
    stats = compute_thresholds(
        _client(context_window=200_000, max_effective_context=100_000), CompactionConfig()
    )
    assert stats.budget == 100_000 - 4_000 - 3_000
    big = compute_thresholds(_client(context_window=100_000), CompactionConfig())
    assert big.budget == 93_000


def test_ut07_52_too_small_is_config_error() -> None:
    """UT07-52 a budget of zero or less, or thresholds out of order, is a ConfigError."""
    tiny = _client(context_window=2_001, max_output_tokens=1_000)
    with pytest.raises(ConfigError, match="context budget too small for client local-30b"):
        compute_thresholds(tiny, CompactionConfig())
    small = _client(context_window=2_030, max_output_tokens=1_000)  # budget 6
    ratios = CompactionConfig(target_ratio=0.1, soft_ratio=0.12, hard_ratio=0.9)
    with pytest.raises(ConfigError, match="context budget too small"):
        compute_thresholds(small, ratios)


# --- PT07-03 ---------------------------------------------------------------------------

# astral (emoji, Gothic), combining mark, zero-width space / joiner, BOM, NUL, controls
_ODD = (0x1F600, 0x10348, 0x65, 0x301, 0x200B, 0x200D, 0xFEFF, 0x0, 0xA, 0x9)
_ADVERSARIAL = st.sampled_from([chr(c) for c in _ODD] + ["e" + chr(0x301)])
_TEXT = st.lists(
    st.one_of(_ADVERSARIAL, st.text(st.characters(exclude_categories=["Cs"]), max_size=20)),
    max_size=12,
).map("".join)
_MESSAGE = st.builds(_msg, _TEXT, st.sampled_from(["user", "assistant"]))


@settings(max_examples=150, deadline=None)
@given(st.lists(_MESSAGE, max_size=8), _MESSAGE, st.integers(min_value=1, max_value=10**6))
def test_pt07_03_estimate_count_matches_and_is_monotone(
    messages: list[Message], extra: Message, budget: int
) -> None:
    """PT07-03 estimate backend equals spec 05 estimate_tokens and never drops on append."""
    counter = TokenCounter(_client())
    count, is_exact = counter.count_messages(messages, budget)
    assert (count, is_exact) == (llm_tokens.estimate_tokens(messages), False)
    assert count >= 0
    longer, _ = counter.count_messages([*messages, extra], budget)
    assert longer >= count
