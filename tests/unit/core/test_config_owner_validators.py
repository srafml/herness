"""Tests for the start-up owner-validator hook (impl 10 U10-109, F10-01 step 3a; UT10-81)."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import structlog
from tests.support.config_tree import write_checked_config

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

LEAK = "secret-ish text"


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    yield
    c.reset_config()
    cv.reset_owner_validators()


@pytest.fixture
def cfg(tmp_path: Path) -> c.HernessConfig:
    return c.load_config("local", config_dir=write_checked_config(tmp_path))


def _issue(cfg: c.HernessConfig, *, offline: bool) -> list[ConfigIssue]:
    return [ConfigIssue("warn", "metrics.catalog", "catalog not built yet", "metrics.yaml")]


def _mapping(cfg: c.HernessConfig, *, offline: bool) -> list[dict[str, str]]:
    return [{"severity": "error", "path": "decisions.primary_decider", "message": "m" * 400}]


def _invalid(cfg: c.HernessConfig, *, offline: bool) -> list[object]:
    return [42, {"severity": "fatal", "path": "x", "message": "y"}]


def _raises(cfg: c.HernessConfig, *, offline: bool) -> list[ConfigIssue]:
    raise RuntimeError(LEAK)


def _register_all() -> None:
    cv.register_owner_validator("metrics.catalog", _issue)
    cv.register_owner_validator("enrich.deciders", _mapping)
    cv.register_owner_validator("broken", _invalid)
    cv.register_owner_validator("jobs.resilience", _raises)


def test_ut10_81_converts_and_sorts(cfg: c.HernessConfig) -> None:
    """UT10-81 issues and mappings convert; invalid results and raises become one error each."""
    _register_all()
    issues = cv.run_owner_validators(cfg, offline=True)
    assert [(i.severity, i.path, i.message[:40], i.file) for i in issues] == [
        ("error", "broken", "validator broken returned an invalid iss", None),
        ("error", "broken", "validator broken returned an invalid iss", None),
        ("error", "decisions.primary_decider", "m" * 40, None),
        ("error", "jobs.resilience", "validator jobs.resilience failed: Runtim", None),
        ("warn", "metrics.catalog", "catalog not built yet", "metrics.yaml"),
    ]
    assert len(issues[2].message) == 300
    assert issues[3].message == "validator jobs.resilience failed: RuntimeError"
    assert all(LEAK not in str(issue) for issue in issues)


def test_ut10_81_offline_flag_and_cap(cfg: c.HernessConfig) -> None:
    """UT10-81 validators get the offline flag; at most 500 results are taken from each."""
    flags: list[bool] = []

    def endless(cfg: c.HernessConfig, *, offline: bool) -> Iterator[ConfigIssue]:
        flags.append(offline)
        while True:
            yield ConfigIssue("warn", "x", "y", None)

    cv.register_owner_validator("endless", endless)
    assert len(cv.run_owner_validators(cfg, offline=False)) == 500
    assert flags == [False]


def test_ut10_81_registration_rules() -> None:
    """UT10-81 duplicate name with another object raises; same object is a no-op; bad names fail."""
    cv.register_owner_validator("enrich.deciders", _mapping)
    cv.register_owner_validator("enrich.deciders", _mapping)
    with pytest.raises(ConfigError, match=r"^duplicate owner validator enrich\.deciders$"):
        cv.register_owner_validator("enrich.deciders", _issue)
    for bad in ("Enrich", "", "9x", "a" * 65, "a b"):
        with pytest.raises(ConfigError, match=r"^invalid owner validator name$"):
            cv.register_owner_validator(bad, _issue)


def test_ut10_81_reset(cfg: c.HernessConfig) -> None:
    """UT10-81 reset_config keeps registrations; reset_owner_validators clears them."""
    cv.register_owner_validator("enrich.deciders", _mapping)
    c.reset_config()
    assert len(cv.run_owner_validators(cfg, offline=True)) == 1
    cv.reset_owner_validators()
    assert cv.run_owner_validators(cfg, offline=True) == []


def test_ut10_81_startup_raises_with_issues(cfg: c.HernessConfig) -> None:
    """UT10-81 F10-01 step 3a: logs each issue and raises ConfigError (exit 3) on an error."""
    _register_all()
    with structlog.testing.capture_logs() as logs, pytest.raises(ConfigError) as info:
        cv.run_startup_validators(cfg)
    assert info.value.message == "owner validation failed"
    assert isinstance(info.value, ConfigError)  # the CLI maps ConfigError to exit 3 (R-46)
    assert len(info.value.issues) == 5
    events = [e for e in logs if e["event"] == "config.validate.issue"]
    assert len(events) == 5
    assert LEAK not in str(logs)


def test_ut10_81_startup_passes_with_warnings(cfg: c.HernessConfig) -> None:
    """UT10-81 warn-only results do not stop start-up; they are returned and logged."""
    cv.register_owner_validator("metrics.catalog", _issue)
    with structlog.testing.capture_logs() as logs:
        issues = cv.run_startup_validators(cfg)
    assert [(i.severity, i.path) for i in issues] == [("warn", "metrics.catalog")]
    assert [e["event"] for e in logs] == ["config.validate.issue"]
