"""Acceptance check of T10-13: the repository config templates validate clean (IT10-01, F10-01).

Full `config/` tree (`tests.support.config_tree.write_repo_config`) with owner defaults,
profile `synth`: `validate(offline=True)` reports no errors.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.config_tree import register_checked_names, write_repo_config

from herness.core import config as c
from herness.core import config_validate as cv
from herness.core import registry

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _reset() -> Iterator[None]:
    register_checked_names()
    yield
    c.reset_config()
    registry.reset_registry()
    cv.reset_owner_validators()


def test_it10_01_synth_offline_validate_reports_no_errors(tmp_path: Path) -> None:
    """IT10-01 F10-01: `validate(offline=True)` on the repo templates, profile synth, no errors."""
    cfg_dir = write_repo_config(tmp_path)
    issues = c.validate(cfg_dir, "synth", offline=True)
    assert [i for i in issues if i.severity == "error"] == []
