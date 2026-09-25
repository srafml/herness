"""Tests for tests/conftest.py: Hypothesis profiles and state reset (U11-35)."""

import os

import pytest
from hypothesis import settings

pytestmark = pytest.mark.unit

INNER = '''
import os

import pytest

pytestmark = pytest.mark.unit


def test_ut99_01_inherited_faults_removed() -> None:
    """UT99-01"""
    assert os.environ["HERNESS_ENV"] == "test"
    assert "HERNESS_FAULTS" not in os.environ


def test_ut99_02_test_may_set_faults(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT99-02"""
    monkeypatch.setenv("HERNESS_FAULTS", "plan.json")
    assert os.environ["HERNESS_FAULTS"] == "plan.json"


def test_ut99_03_faults_gone_again() -> None:
    """UT99-03"""
    assert "HERNESS_FAULTS" not in os.environ
'''


def test_ut11_37_profiles_and_state_reset(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT11-37 `commit` and `nightly` profiles exist; each test starts in a reset state.

    The registry half of UT11-37 waits for T10-04 (herness.core.registry.reset_registry).
    """
    commit = settings.get_profile("commit")
    nightly = settings.get_profile("nightly")
    assert (commit.max_examples, commit.derandomize) == (200, True)
    assert commit.deadline is not None
    assert commit.deadline.total_seconds() == 0.5
    assert (nightly.max_examples, nightly.derandomize, nightly.deadline) == (10_000, True, None)
    assert os.environ["HERNESS_ENV"] == "test"
    monkeypatch.setenv("HERNESS_FAULTS", "inherited.json")
    monkeypatch.setenv("HERNESS_ENV", "dev")
    pytester.makepyfile(test_inner=INNER)
    result = pytester.runpytest("-p", "tests.conftest", "-p", "no:asyncio")
    result.assert_outcomes(passed=3)
