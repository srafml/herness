"""Tests for herness.harness.llm.base: resolve_request_params, egress_purpose_for (U05-21)."""

from __future__ import annotations

from decimal import Decimal
from typing import Literal

import pytest

from herness.harness.llm import base
from herness.harness.llm.settings import ClientConfig, ClientSupports, PricePerMTok, RoleParams

pytestmark = pytest.mark.unit

_ZERO = Decimal("0")
_ONE = Decimal("1")
_ZERO_PRICE = PricePerMTok(input=_ZERO, output=_ZERO, cache_read=_ZERO, cache_write=_ZERO)
_ANTHROPIC_PRICE = PricePerMTok(input=_ONE, output=_ONE, cache_read=_ZERO, cache_write=_ZERO)


def _openai_client(
    *, thinking_toggle: bool = True, effort: bool = True, sampling_params: bool = True
) -> ClientConfig:
    return ClientConfig(
        name="local-30b",
        kind="openai_compat",
        base_url="http://127.0.0.1:8000/v1",
        model="local-30b",
        context_window=32768,
        max_output_tokens=4096,
        tokenizer="vllm_endpoint",
        max_concurrency=4,
        price_per_mtok=_ZERO_PRICE,
        supports=ClientSupports(
            thinking_toggle=thinking_toggle, effort=effort, sampling_params=sampling_params
        ),
    )


def _anthropic_client(
    *,
    thinking_mode: Literal["adaptive_always", "adaptive_optional", "budget"],
    effort: bool = True,
    sampling_params: bool = True,
) -> ClientConfig:
    return ClientConfig(
        name="claude-opus-5-5",
        kind="anthropic",
        model="claude-opus-5-5",
        context_window=200_000,
        max_output_tokens=8192,
        tokenizer="anthropic",
        max_concurrency=4,
        thinking_mode=thinking_mode,
        price_per_mtok=_ANTHROPIC_PRICE,
        supports=ClientSupports(effort=effort, sampling_params=sampling_params),
    )


def _params(
    thinking: Literal["off", "on", "auto"],
    *,
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None,
    temperature: float | None = None,
) -> RoleParams:
    return RoleParams(thinking=thinking, effort=effort, temperature=temperature)


_TOGGLE_ON = _openai_client()
_TOGGLE_OFF = _openai_client(thinking_toggle=False)
_NO_EFFORT = _openai_client(effort=False)
_NO_SAMPLING = _openai_client(sampling_params=False)
_ALWAYS = _anthropic_client(thinking_mode="adaptive_always")
_OPTIONAL = _anthropic_client(thinking_mode="adaptive_optional")
_BUDGET = _anthropic_client(thinking_mode="budget")

_FALLBACK = _params("off", temperature=1.0)

# Each row is a depth x role x client combination and the ResolvedParams fields it expects.
_ROWS: list[
    tuple[str, str, str, RoleParams, ClientConfig, tuple[str, str | None, float | None, bool]]
] = [
    (
        "auto_on_fast_planner",
        "fast",
        "planner",
        _params("auto", temperature=0.7),
        _TOGGLE_ON,
        ("on", "medium", 0.7, False),
    ),
    (
        "auto_off_fast_chat",
        "fast",
        "chat",
        _params("auto", temperature=0.5),
        _TOGGLE_ON,
        ("off", "medium", 0.5, False),
    ),
    (
        "auto_on_deep_writer",
        "deep",
        "writer",
        _params("auto", effort="high", temperature=0.3),
        _TOGGLE_ON,
        ("on", "high", 0.3, False),
    ),
    (
        "auto_off_standard_writer",
        "standard",
        "writer",
        _params("auto", temperature=0.3),
        _TOGGLE_ON,
        ("off", "medium", 0.3, False),
    ),
    (
        "base_role_judge_is_planner",
        "fast",
        "judge",
        _params("auto"),
        _TOGGLE_ON,
        ("on", "medium", None, False),
    ),
    (
        "base_role_chat_off_hours_is_chat",
        "fast",
        "chat_off_hours",
        _params("auto", temperature=0.2),
        _TOGGLE_ON,
        ("off", "medium", 0.2, False),
    ),
    (
        "base_role_skeptic_final_is_skeptic",
        "deep",
        "skeptic_final",
        _params("auto", temperature=0.1),
        _TOGGLE_ON,
        ("on", "medium", 0.1, False),
    ),
    (
        "explicit_on_overrides_auto_off",
        "fast",
        "chat",
        _params("on", temperature=0.4),
        _TOGGLE_ON,
        ("on", "medium", 0.4, False),
    ),
    (
        "explicit_off_overrides_auto_on",
        "deep",
        "planner",
        _params("off", temperature=0.4),
        _TOGGLE_ON,
        ("off", "medium", 0.4, False),
    ),
    (
        "toggle_off_forces_off",
        "fast",
        "planner",
        _params("on", temperature=0.4),
        _TOGGLE_OFF,
        ("off", "medium", 0.4, False),
    ),
    (
        "no_effort_support_gives_none",
        "fast",
        "planner",
        _params("auto", effort="high", temperature=0.4),
        _NO_EFFORT,
        ("on", None, 0.4, False),
    ),
    (
        "no_sampling_support_gives_none_temp",
        "fast",
        "planner",
        _params("auto", temperature=0.9),
        _NO_SAMPLING,
        ("on", "medium", None, False),
    ),
    (
        "anthropic_always_downgrades_chat",
        "fast",
        "chat",
        _params("auto", temperature=0.6),
        _ALWAYS,
        ("on", "low", 0.6, True),
    ),
    (
        "anthropic_always_no_downgrade_planner",
        "fast",
        "planner",
        _params("auto", effort="high", temperature=0.6),
        _ALWAYS,
        ("on", "high", 0.6, False),
    ),
    (
        "anthropic_optional_does_not_force_on",
        "fast",
        "chat",
        _params("auto", temperature=0.6),
        _OPTIONAL,
        ("off", "medium", 0.6, False),
    ),
    (
        "anthropic_budget_thinking_on_drops_temperature",
        "fast",
        "planner",
        _params("on", temperature=0.6),
        _BUDGET,
        ("on", "medium", None, False),
    ),
    (
        "anthropic_budget_thinking_off_keeps_temperature",
        "fast",
        "chat",
        _params("off", temperature=0.6),
        _BUDGET,
        ("off", "medium", 0.6, False),
    ),
]


@pytest.mark.parametrize(
    ("depth", "model_role", "role_params", "client", "expected"),
    [row[1:] for row in _ROWS],
    ids=[row[0] for row in _ROWS],
)
def test_ut05_19_resolve_request_params(
    depth: Literal["fast", "standard", "deep"],
    model_role: str,
    role_params: RoleParams,
    client: ClientConfig,
    expected: tuple[str, str | None, float | None, bool],
) -> None:
    """UT05-19 depth x role x client table: thinking, effort, temperature and downgraded."""
    result = base.resolve_request_params(
        model_role=model_role,
        depth=depth,
        client=client,
        role_params=role_params,
        fallback=_FALLBACK,
    )
    thinking, effort, temperature, downgraded = expected
    assert result.thinking == thinking
    assert result.effort == effort
    assert result.temperature == temperature
    assert result.downgraded is downgraded


def test_ut05_19_thinking_is_never_auto() -> None:
    """UT05-19 the resolved thinking is always on or off, never auto (postcondition)."""
    result = base.resolve_request_params(
        model_role="chat", depth="fast", client=_TOGGLE_ON, role_params=None, fallback=_FALLBACK
    )
    assert result.thinking in ("on", "off")


def test_ut05_19_none_role_params_uses_fallback() -> None:
    """UT05-19 role_params=None falls back to the caller-supplied RoleSpec values."""
    result = base.resolve_request_params(
        model_role="chat", depth="fast", client=_TOGGLE_ON, role_params=None, fallback=_FALLBACK
    )
    assert result.thinking == "off"
    assert result.temperature == 1.0


@pytest.mark.parametrize(
    ("model_role", "expected"),
    [
        ("writer", "reasoning_final"),
        ("skeptic_final", "reasoning_final"),
        ("chat", "reasoning"),
        ("planner", "reasoning"),
        ("skeptic", "reasoning"),
        ("some_unknown_role", "reasoning"),
    ],
)
def test_ut05_20_egress_purpose_for(model_role: str, expected: str) -> None:
    """UT05-20 writer and skeptic_final are reasoning_final; every other role is reasoning."""
    assert base.egress_purpose_for(model_role) == expected
