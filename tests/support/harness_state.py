"""The `reset_harness_state` fixture (impl 05 T05-16, §2 module-level state note).

Resets the process-wide `ToolRegistry` of `herness.harness.tools` before and after every
test, so one test's registrations never leak into another. The module is only touched when
it is already imported, so tests that never load the harness pay nothing.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator

import pytest

_TOOLS_MODULE = "herness.harness.tools"


def reset_tool_registry() -> None:
    """Drop the process `ToolRegistry`; the next `tool_registry()` call builds a new one."""
    module = sys.modules.get(_TOOLS_MODULE)
    if module is not None:
        module._reset_tool_registry()


@pytest.fixture(autouse=True)
def reset_harness_state() -> Iterator[None]:
    """A fresh process tool registry before and after each test (T05-16)."""
    reset_tool_registry()
    yield
    reset_tool_registry()
