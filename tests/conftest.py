"""Root conftest (U11-35): plugins, Hypothesis profiles, ignore list, state reset.

The remaining `tests.support` plugins of U11-35 (`fake_clock`, `builds`, `bench`,
`truth`, `ops_store`) are appended here by the cards that create them (T11-03,
T11-40 and later). `pytester` is loaded for the plugin's own tests (UT11-31..UT11-37).
"""

from datetime import timedelta

import pytest
from hypothesis import settings

pytest_plugins = ["pytester", "tests.support.plugin"]

collect_ignore = ["support", "fixtures"]

settings.register_profile(
    "commit", max_examples=200, derandomize=True, deadline=timedelta(milliseconds=500)
)
settings.register_profile("nightly", max_examples=10_000, derandomize=True, deadline=None)
# Hypothesis' own pytest plugin loads `--hypothesis-profile` in `pytest_configure`,
# which runs after this import, so a profile given on the command line wins.
settings.load_profile("commit")


@pytest.fixture(autouse=True)
def reset_herness_state(monkeypatch: pytest.MonkeyPatch) -> None:
    """Give every test `HERNESS_ENV=test` and no inherited `HERNESS_FAULTS`.

    The registry and config resets of U11-35 (`reset_registry`, `reset_config`,
    T10-04 and T10-03) are added when those modules exist.
    """
    monkeypatch.setenv("HERNESS_ENV", "test")
    monkeypatch.delenv("HERNESS_FAULTS", raising=False)
