"""Tool dispatch overhead benchmark (BT05-02).

Run: pytest -m "integration and slow" tests/bench.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.dispatch_standin import (
    SyncTool,
    call,
    make_state,
    make_tool_ctx,
    strict_schema,
    use_test_config,
)

from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.harness.tools import dispatch

pytestmark = [pytest.mark.integration, pytest.mark.slow]

CALLS = 1_000
WARM_UP = 50


@pytest.fixture
def cfg(tmp_path: Path, reset_process_state: ProcessState) -> Iterator[None]:
    del reset_process_state
    use_test_config(tmp_path)
    yield
    c.reset_config()


def test_bt05_02_dispatch_overhead_per_call(cfg: None) -> None:
    """BT05-02 `dispatch` with 1 no-op sync tool, 1,000 calls: mean < 5 ms per call."""
    del cfg
    tool = SyncTool("get_metric", schema=strict_schema({"n": {"type": "integer"}}))
    tools = {"get_metric": tool}
    ctx, state = make_tool_ctx(), make_state()

    async def rounds() -> float:
        for i in range(WARM_UP):
            await dispatch(ctx, tools, [call("get_metric", f"w{i}", n=-1 - i)], None, state)
        start = time.perf_counter()
        for i in range(CALLS):
            await dispatch(ctx, tools, [call("get_metric", f"c{i}", n=i)], None, state)
        return (time.perf_counter() - start) / CALLS

    mean_ms = asyncio.run(rounds()) * 1000
    sys.stderr.write(f"BT05-02 dispatch mean {mean_ms:.3f} ms per call\n")
    assert len(tool.calls) == CALLS + WARM_UP
    assert mean_ms < 5, f"dispatch mean {mean_ms:.3f} ms"
