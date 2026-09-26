"""`fault_env` fixture (impl 08 §11, R-40): a JSON fault plan enabled for one test.

Calling the fixture value writes the rules as JSON to `tmp_path`, sets `HERNESS_ENV=test`
and `HERNESS_FAULTS`, and marks the fault plan not loaded, so the next
`herness.core.resilience.fault_point` call loads it. The process state is the one of the
`reset_process_state` fixture (seeded `rng`, no-op sleeps).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

import pytest

from herness.core.resilience import ProcessState
from herness.core.resilience._state import NOT_LOADED

type FaultEnv = Callable[[Sequence[Mapping[str, object]]], Path]


@pytest.fixture
def fault_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> FaultEnv:
    """Return `enable(rules) -> plan path`; each call replaces the active plan."""

    def enable(rules: Sequence[Mapping[str, object]]) -> Path:
        path = tmp_path / "fault_plan.json"
        path.write_text(json.dumps(list(rules)), encoding="utf-8")
        monkeypatch.setenv("HERNESS_ENV", "test")
        monkeypatch.setenv("HERNESS_FAULTS", str(path))
        reset_process_state.fault_plan = NOT_LOADED
        reset_process_state.faults_enabled = False
        return path

    return enable
