"""Token counting per backend and context thresholds (impl 07 U07-68, U07-69; design 07 §5.2, §5.3).

Built only on spec 05's one estimator and counter (R-17): ``herness.harness.llm.tokens``
``estimate_tokens`` / ``count_tokens`` and ``LoopState.est_input_tokens()``. Anthropic counts
leave the host only through spec 05's counter and the egress guard (R-55, TH07-14).
"""

import math
from collections.abc import Callable
from typing import Final

from herness.core.errors import ConfigError
from herness.core.ids import canonical_json, sha256_hex
from herness.core.types import LoopState, Message, SystemBlock, ToolSpec
from herness.harness.llm import tokens as llm_tokens
from herness.harness.llm.settings import ClientConfig
from herness.harness.memory.settings import CompactionConfig
from herness.harness.memory.types import ContextStats

__all__ = ["CACHE_MAX", "TokenCounter", "compute_thresholds"]

type ExactCounter = Callable[
    [ClientConfig, list[Message], list[ToolSpec], list[SystemBlock]], tuple[int, bool]
]

TOKENIZERS: Final = frozenset({"vllm_endpoint", "estimate", "anthropic"})
CACHE_MAX: Final = 10_000  # per-message exact counts kept (vLLM only); oldest evicted
_EXACT_NUM, _EXACT_DEN = 3, 5  # anthropic asks for an exact count at t >= 0.6 x budget
SAFETY_MIN: Final = 1024
SAFETY_PER_100: Final = 3  # safety = max(1024, floor(0.03 m))


def _message_key(message: Message) -> str:
    return sha256_hex(canonical_json(message.model_dump(mode="json")))


class TokenCounter:
    """Counts prompt tokens for one client (U07-68); one per ``ContextCompactor``.

    Not thread-safe. ``exact`` in a result is true only for a backend count without fallback.
    """

    def __init__(
        self,
        cfg: ClientConfig,
        *,
        exact: ExactCounter = llm_tokens.count_tokens,
        estimate: Callable[..., int] = llm_tokens.estimate_tokens,
    ) -> None:
        if cfg.tokenizer not in TOKENIZERS:
            msg = f"unknown tokenizer for client {cfg.name}"
            raise ConfigError(msg)
        self._cfg = cfg
        self._exact = exact
        self._estimate = estimate
        self._cache: dict[str, int] = {}  # insertion ordered: the first key is the oldest

    def _near_budget(self, tokens: int, budget: int) -> bool:
        return tokens * _EXACT_DEN >= budget * _EXACT_NUM  # t >= 0.6 x budget, no float

    def count_state(self, state: LoopState, budget: int) -> tuple[int, bool]:
        """Tokens of the live loop state: last usage plus the estimate of newer messages."""
        tokenizer = self._cfg.tokenizer
        if tokenizer == "vllm_endpoint":
            return self._exact(self._cfg, state.messages, [], [])
        tokens = state.est_input_tokens()
        if tokenizer == "anthropic" and self._near_budget(tokens, budget):
            return self._exact(self._cfg, state.messages, [], [])
        return tokens, False

    def count_messages(self, messages: list[Message], budget: int) -> tuple[int, bool]:
        """Tokens of a candidate message list (built by compaction; no usage yet)."""
        tokenizer = self._cfg.tokenizer
        if tokenizer == "vllm_endpoint":
            return self._vllm_sum(messages)
        tokens = self._estimate(messages)
        if tokenizer == "anthropic" and self._near_budget(tokens, budget):
            return self._exact(self._cfg, messages, [], [])
        return tokens, False

    def _vllm_sum(self, messages: list[Message]) -> tuple[int, bool]:
        total, all_exact = 0, True
        for message in messages:
            key = _message_key(message)
            cached = self._cache.get(key)
            if cached is not None:
                total += cached
                continue
            count, exact = self._exact(self._cfg, [message], [], [])
            total += count
            if not exact:
                all_exact = False  # a fallback estimate is not cached; asked again next time
                continue
            if len(self._cache) >= CACHE_MAX:
                del self._cache[next(iter(self._cache))]
            self._cache[key] = count
        return total, all_exact


def compute_thresholds(
    cfg: ClientConfig, ratios: CompactionConfig, tokens: int = 0, exact: bool = False
) -> ContextStats:
    """Budget and soft, hard and target thresholds for one client (U07-69)."""
    window = cfg.context_window
    usable = min(window, cfg.max_effective_context or window)
    safety = max(SAFETY_MIN, usable * SAFETY_PER_100 // 100)
    budget = usable - cfg.max_output_tokens - safety
    soft = math.floor(ratios.soft_ratio * budget)
    hard = math.floor(ratios.hard_ratio * budget)
    target = math.floor(ratios.target_ratio * budget)
    if budget <= 0 or not 0 < target < soft < hard < budget:
        msg = f"context budget too small for client {cfg.name}"
        raise ConfigError(msg)
    return ContextStats(
        tokens=tokens, exact=exact, budget=budget, soft=soft, hard=hard, target=target
    )
