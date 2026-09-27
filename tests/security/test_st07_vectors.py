"""ST07-09: LanceDB filter injection via memory ids, layers and statuses (TH07-09)."""

from collections.abc import Callable
from typing import Any

import numpy as np
import pytest

from herness.core.errors import ToolInputError
from herness.harness.memory.store import VectorIndex, VectorRow
from herness.store.vectors import EMBEDDING_DIM

pytestmark = pytest.mark.unit

GOOD = "mem_01ARZ3NDEKTSV4RRFFQ69G5FAV"
ATTACKS = [
    "' OR 1=1 --",
    "mem_x' OR 1=1 --",
    GOOD + "'--",
    GOOD + "' OR '1'='1",
    GOOD + " OR 1=1",
    GOOD + "\n",
    "mem_01ARZ3NDEKTSV4RRFFQ69G5FA'",
    "semantic') OR ('1'='1",
    "active' --",
    "",
    # Unicode lookalikes, case variants and control characters (not in any allowlist).
    "mem_" + "\uff10" * 26,  # fullwidth digits
    "mem_" + "\u0660" * 26,  # Arabic-Indic digits
    "\u0430ctive",  # Cyrillic a
    "ACTIVE",
    "Semantic",
    "active\n",
    "active\x00",
    GOOD[:-1] + "\x00",
    GOOD + "\x00",
]


class SpyFactory:
    """A store_factory that records every call; the tests assert it is never reached."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        msg = "LanceDB must not be reached"
        raise AssertionError(msg)


def _vec() -> np.ndarray:
    vec = np.zeros(EMBEDDING_DIM, dtype=np.float32)
    vec[0] = 1.0
    return vec


def _ops(idx: VectorIndex, bad: Any) -> dict[str, Callable[[], object]]:
    return {
        "set_status_ids": lambda: idx.set_status([GOOD, bad], "active"),
        "set_status_status": lambda: idx.set_status([GOOD], bad),
        "delete": lambda: idx.delete([GOOD, bad]),
        "vectors": lambda: idx.vectors([bad, GOOD]),
        "search_layers": lambda: idx.search(_vec(), ["semantic", bad], ["active"], 5),
        "search_statuses": lambda: idx.search(_vec(), ["semantic"], [bad], 5),
    }


@pytest.mark.parametrize("bad", ATTACKS)
def test_st07_09_filter_injection_rejected_before_lancedb(bad: str) -> None:
    """ST07-09 quote, comment and OR 1=1 payloads raise ToolInputError; LanceDB is not called."""
    spy = SpyFactory()
    idx = VectorIndex(spy)
    for name, call in _ops(idx, bad).items():
        with pytest.raises(ToolInputError, match="invalid id in vector filter") as info:
            call()
        assert bad not in str(info.value) or bad == "", name
    if bad:
        with pytest.raises(ToolInputError, match="invalid id in vector filter"):
            idx.list_ids(bad, 10)
        row = VectorRow(bad, "semantic", "glossary", "active", "h", "m", _vec())
        with pytest.raises(ToolInputError, match="invalid vector row"):
            idx.upsert([row])
    assert spy.calls == 0


@pytest.mark.parametrize("bad", [1, None, b"mem_x", ["mem_x"]])
def test_st07_09_non_string_filter_values_rejected(bad: Any) -> None:
    """ST07-09 non-string values in id, layer or status lists are rejected before LanceDB."""
    spy = SpyFactory()
    idx = VectorIndex(spy)
    for call in _ops(idx, bad).values():
        with pytest.raises(ToolInputError, match="invalid id in vector filter"):
            call()
    with pytest.raises(ToolInputError, match="invalid id in vector filter"):
        idx.list_ids(bad, 10)
    assert spy.calls == 0


def test_st07_09_bare_string_is_not_an_id_list() -> None:
    """ST07-09 a bare string passed as an id sequence is rejected, not iterated per character."""
    spy = SpyFactory()
    idx = VectorIndex(spy)
    with pytest.raises(ToolInputError, match="invalid id in vector filter"):
        idx.delete(GOOD)
    with pytest.raises(ToolInputError, match="invalid id in vector filter"):
        idx.search(_vec(), "semantic", ["active"], 5)  # type: ignore[arg-type]
    assert spy.calls == 0
