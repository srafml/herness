"""Controller-verification rows of T11-32 for `ScriptedRegistry` (U11-41 spec note).

A `--mock-llm` run drives the real spec 05 loop, which refuses a client whose name differs
from its `ClientConfig` (`run_agent`), and the spec 08 model chain, which drops a GPU client
whose class is not loaded. A mock run holds no GPU (`eval_gpu_class` is `none`), so the
registry hands out the scripted client under the requested key and its configs carry no
GPU class.
"""

from __future__ import annotations

from typing import Any

import pytest

from herness.core.types import LLMRequest, Message, RequestMeta, SystemBlock, TextPart
from herness.eval.scripted import LLMScript, ScriptBook
from herness.eval.scripted_client import ScriptedLLMClient, ScriptedRegistry
from herness.harness.llm.registry import LLMRegistry
from herness.harness.llm.settings import ModelsConfig

pytestmark = pytest.mark.unit

_ZERO = {"input": "0", "output": "0", "cache_read": "0", "cache_write": "0"}


def _inner(gpu_class: str | None = None) -> LLMRegistry:
    client: dict[str, Any] = {
        "kind": "openai_compat", "base_url": "http://127.0.0.1:8000/v1", "model": "local",
        "context_window": 32_768, "max_output_tokens": 4_096, "tokenizer": "estimate",
        "max_concurrency": 4, "price_per_mtok": dict(_ZERO),
    }  # fmt: skip
    if gpu_class is not None:
        client["gpu_class"] = gpu_class
    cfg = ModelsConfig.model_validate({
        "models": {
            "clients": {"local-30b": client, "local-large": {**client, "model": "large"}},
            "roles": {"writer": "local-30b"}, "fallback": {"writer": ["local-30b", "local-large"]},
        },
        "harness": {},
    })  # fmt: skip
    return LLMRegistry(cfg, profile="test", egress_enabled=False)


def _book() -> ScriptBook:
    turns = [{"final": {"text": "first"}}, {"final": {"text": "second"}}]
    return ScriptBook([LLMScript.model_validate(
        {"match": {"role": "writer"}, "turns": turns, "source": "t#0"}
    )])  # fmt: skip


def _req(client: str) -> LLMRequest:
    meta = RequestMeta.model_validate({
        "run_id": "run_1", "task_id": None, "role": "writer", "model_role": "writer",
        "step": 0, "request_key": "k",
    })  # fmt: skip
    return LLMRequest(
        client=client, system=[SystemBlock(text="Write.")],
        messages=[Message(role="user", parts=[TextPart(text="Go.")])],
        max_output_tokens=64, timeout_s=30, metadata=meta,
    )  # fmt: skip


def test_cv_t11_32_client_is_named_after_the_key_and_shares_the_book() -> None:
    """CV-T11-32 `client(key)` is a scripted client named `key`; every key answers from the
    same script book (one turn counter per role and dedup key for the whole run)."""
    scripted = ScriptedLLMClient(_book(), name="local-30b")
    registry = ScriptedRegistry(_inner(), scripted)
    assert registry.client("local-30b") is scripted
    other = registry.client("local-large")
    assert isinstance(other, ScriptedLLMClient)
    assert other.name == "local-large"
    assert registry.client("local-large") is other  # cached per key
    texts = [registry.client(k).complete(_req(k)).text for k in ("local-30b", "local-large")]
    assert texts == ["first", "second"]


def test_cv_t11_32_config_drops_the_gpu_class() -> None:
    """CV-T11-32 `config(key)` is the inner config without a GPU class; the rest is equal."""
    inner = _inner(gpu_class="reasoning")
    real = inner.config("local-30b")
    assert real.gpu_class == "reasoning"
    got = ScriptedRegistry(inner, ScriptedLLMClient(_book())).config("local-30b")
    assert got.gpu_class is None
    assert got.model_dump(exclude={"gpu_class"}) == real.model_dump(exclude={"gpu_class"})
