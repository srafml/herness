"""The `reset_harness_state` fixture (impl 05 T05-16, §2 module-level state note).

Resets the process-wide `ToolRegistry` of `herness.harness.tools` and the process
`MemoryStore` of `herness.harness.memory` (impl 07 U07-98: "the spec 11 fixture calls
`_reset_memory_store()`", T07-23) before and after every test, so one test's registrations
never leak into another. A module is only touched when it is already imported, so tests that
never load the harness pay nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

_TOOLS_MODULE = "herness.harness.tools"
_MEMORY_MODULE = "herness.harness.memory"


def reset_tool_registry() -> None:
    """Drop the process `ToolRegistry`; the next `tool_registry()` call builds a new one."""
    module = sys.modules.get(_TOOLS_MODULE)
    if module is not None:
        module._reset_tool_registry()


def reset_memory_store() -> None:
    """Drop the process `MemoryStore` and the memory job-handler seams (T07-23)."""
    module = sys.modules.get(_MEMORY_MODULE)
    if module is not None:
        module._reset_memory_store()


@pytest.fixture(autouse=True)
def reset_harness_state() -> Iterator[None]:
    """A fresh process tool registry and memory store before and after each test."""
    reset_tool_registry()
    reset_memory_store()
    yield
    reset_tool_registry()
    reset_memory_store()
