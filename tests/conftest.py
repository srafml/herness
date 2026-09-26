"""Root conftest (U11-35): plugins, Hypothesis profiles, ignore list, state reset.

`pytester` is loaded for the plugin's own tests (UT11-31..UT11-37). T08-03 registers the
opt-in `reset_process_state` fixture (impl 08 §11) and T08-08 the `tests.support.fault_env`
plugin; T11-40 registers `tests.support.ops_store` (U11-78) in place of the interim
`tests.support.ops_core_store` (T02-04).
"""

import random
from collections.abc import Iterator
from datetime import timedelta

import pytest
from hypothesis import settings

from herness.core.config import reset_config
from herness.core.config_validate import reset_owner_validators
from herness.core.registry import reset_registry
from herness.core.resilience import ProcessState
from herness.core.resilience import reset_process_state as _reset

pytest_plugins = [
    "pytester",
    "tests.support.plugin",
    "tests.support.fake_clock",
    "tests.support.ops_store",
    "tests.support.fake_keyring",
    "tests.support.fault_env",
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
def reset_herness_state(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Give every test `HERNESS_ENV=test`, no inherited `HERNESS_FAULTS`, and a cleared
    registry, config cache and owner-validator registrations before and after (U11-35;
    T10-12 carry-over)."""
    reset_registry()
    reset_config()
    reset_owner_validators()
    monkeypatch.setenv("HERNESS_ENV", "test")
    monkeypatch.delenv("HERNESS_FAULTS", raising=False)
    yield
    reset_registry()
    reset_config()
    reset_owner_validators()


@pytest.fixture
def reset_process_state() -> Iterator[ProcessState]:
    """Fresh 08 `ProcessState` with a seeded `rng` and no-op sleeps; reset again afterwards."""
    state = _reset()
    state.rng = random.Random(0)
    state.sleep = lambda _seconds: None
    state.asleep = _no_asleep
    yield state
    _reset()
