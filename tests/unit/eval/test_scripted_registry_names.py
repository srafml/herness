"""Controller-verification rows of T11-32 for `ScriptedRegistry` (U11-41 spec note).

A `--mock-llm` run drives the real spec 05 loop, which refuses a client whose name differs
from its `ClientConfig` (`run_agent`), and the spec 08 model chain, which drops a GPU client
whose class is not loaded. A mock run holds no GPU (`eval_gpu_class` is `none`), so the
registry hands out the scripted client under the requested key and its configs carry no
GPU class.
"""

from __future__ import annotations

import pytest
from tests.unit.eval.test_scripted_client import _book, _inner_registry, _scripts

from herness.eval.scripted_client import ScriptedLLMClient, ScriptedRegistry

pytestmark = pytest.mark.unit


def test_cv_t11_32_client_is_named_after_the_key_and_shares_the_book() -> None:
    """CV-T11-32 `client(key)` is a scripted client named `key` over the same script book."""
    book = _book(*_scripts())
    scripted = ScriptedLLMClient(book, name="local-30b")
    registry = ScriptedRegistry(_inner_registry(), scripted)
    assert registry.client("local-30b") is scripted
    other = registry.client("local-large")
    assert isinstance(other, ScriptedLLMClient)
    assert other.name == "local-large"
    assert other._book is book  # one call counter per (role, dedup_key) for the whole run
    assert registry.client("local-large") is other  # cached per key


def test_cv_t11_32_config_drops_the_gpu_class() -> None:
    """CV-T11-32 `config(key)` is the inner config without a GPU class; the rest is equal."""
    inner = _inner_registry()
    registry = ScriptedRegistry(inner, ScriptedLLMClient(_book(*_scripts())))
    real = inner.config("local-30b").model_copy(update={"gpu_class": "reasoning"})
    inner_config = inner.config

    def gpu_config(name: str) -> object:
        return real if name == "local-30b" else inner_config(name)

    inner.config = gpu_config  # type: ignore[method-assign,assignment]
    got = registry.config("local-30b")
    assert got.gpu_class is None
    assert got.model_dump(exclude={"gpu_class"}) == real.model_dump(exclude={"gpu_class"})
