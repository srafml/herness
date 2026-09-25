"""Repository-wide test fixtures.

T11-01 owns this file; T08-03 registers only the `reset_process_state` fixture (impl 08 §11).
"""

import random
from collections.abc import Iterator

import pytest

from herness.core.resilience import ProcessState
from herness.core.resilience import reset_process_state as _reset


async def _no_asleep(_seconds: float) -> None:
    return None


@pytest.fixture
def reset_process_state() -> Iterator[ProcessState]:
    """Fresh 08 `ProcessState` with a seeded `rng` and no-op sleeps; reset again afterwards."""
    state = _reset()
    state.rng = random.Random(0)
    state.sleep = lambda _seconds: None
    state.asleep = _no_asleep
    yield state
    _reset()
