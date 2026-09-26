"""Root conftest (U11-35): plugins, Hypothesis profiles, ignore list, state reset.

The remaining `tests.support` plugins of U11-35 (`builds`, `bench`, `truth`, `ops_store`)
are appended here by the cards that create them (T11-40 and later). `pytester` is loaded
for the plugin's own tests (UT11-31..UT11-37).
T08-03 registers the opt-in `reset_process_state` fixture (impl 08 §11).
"""

import random
from collections.abc import Iterator
from datetime import timedelta

import pytest
from hypothesis import settings

from herness.core.resilience import ProcessState
from herness.core.resilience import reset_process_state as _reset

# T02-04 added `tests.support.ops_core_store`; T11-40 replaces it with `tests.support.ops_store`.
pytest_plugins = [
    "pytester",
    "tests.support.plugin",
    "tests.support.fake_clock",
    "tests.support.ops_core_store",
    "tests.support.fake_keyring",
]

collect_ignore = ["support", "fixtures"]

settings.register_profile(
    "commit", max_examples=200, derandomize=True, deadline=timedelta(milliseconds=500)
)
settings.register_profile("nightly", max_examples=10_000, derandomize=True, deadline=None)
# Hypothesis' own pytest plugin loads `--hypothesis-profile` in `pytest_configure`,
# which runs after this import, so a profile given on the command line wins.
settings.load_profile("commit")


async def _no_asleep(_seconds: float) -> None:
    return None


@pytest.fixture(autouse=True)
def reset_herness_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test `HERNESS_ENV=test` and no inherited `HERNESS_FAULTS`.

    The registry and config resets of U11-35 (`reset_registry`, `reset_config`,
    T10-04 and T10-03) are added when those modules exist.
    """
    monkeypatch.setenv("HERNESS_ENV", "test")
    monkeypatch.delenv("HERNESS_FAULTS", raising=False)


@pytest.fixture
def reset_process_state() -> Iterator[ProcessState]:
    """Fresh 08 `ProcessState` with a seeded `rng` and no-op sleeps; reset again afterwards."""
    state = _reset()
    state.rng = random.Random(0)
    state.sleep = lambda _seconds: None
    state.asleep = _no_asleep
    yield state
    _reset()
