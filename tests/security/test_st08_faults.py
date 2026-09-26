"""Security tests for the fault hook (impl 08 ST08-06, TH08-06; U08-33, U08-34)."""

from __future__ import annotations

import ast
import json
import os
from pathlib import Path

import pytest
import structlog

from herness.core.errors import ConfigError
from herness.core.resilience import ProcessState, fault_point, faults

pytestmark = pytest.mark.unit

_PAYLOAD = '- !!python/object/apply:os.system ["echo pwned > {marker}"]\n'
_VALID = [{"point": "llm.call", "action": "error:StoreBusy"}]


@pytest.fixture
def no_exec(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """Record any `os.system` call (none may happen) instead of running it."""
    calls: list[object] = []
    monkeypatch.setattr(os, "system", lambda command: calls.append(command) or 0)
    return calls


def _enable(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("HERNESS_ENV", "test")
    monkeypatch.setenv("HERNESS_FAULTS", str(path))


def _assert_refused(state: ProcessState) -> None:
    with pytest.raises(ConfigError, match="invalid fault plan"):
        fault_point("llm.call")
    assert state.faults_enabled is False


def test_st08_06_symlink_plan_refused(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST08-06 HERNESS_FAULTS pointing to a symlink raises ConfigError."""
    target = tmp_path / "real.json"
    target.write_text(json.dumps(_VALID), encoding="utf-8")
    link = tmp_path / "plan.json"
    try:
        os.symlink(target, link)
    except OSError:  # no symlink privilege: drive the same branch through is_symlink
        link = target
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == "real.json")
    _enable(monkeypatch, link)
    _assert_refused(reset_process_state)


def test_st08_06_one_megabyte_plan_refused(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST08-06 a 1 MB plan raises ConfigError."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps([{**_VALID[0], "model": "x" * 1_048_576}]), encoding="utf-8")
    _enable(monkeypatch, path)
    _assert_refused(reset_process_state)


def test_st08_06_yaml_object_apply_not_executed(
    tmp_path: Path,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    no_exec: list[object],
) -> None:
    """ST08-06 a .yaml plan with !!python/object/apply raises ConfigError; nothing runs."""
    marker = tmp_path / "pwned.txt"
    path = tmp_path / "plan.yaml"
    path.write_text(_PAYLOAD.format(marker=marker), encoding="utf-8")
    _enable(monkeypatch, path)
    _assert_refused(reset_process_state)
    assert no_exec == []
    assert not marker.exists()


def test_st08_06_json_suffix_yaml_body_not_executed(
    tmp_path: Path,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
    no_exec: list[object],
) -> None:
    """ST08-06 the same YAML payload named .json fails JSON parsing; nothing runs."""
    path = tmp_path / "plan.json"
    path.write_text(_PAYLOAD.format(marker=tmp_path / "m"), encoding="utf-8")
    _enable(monkeypatch, path)
    _assert_refused(reset_process_state)
    assert no_exec == []


def test_st08_06_no_yaml_parser_in_faults() -> None:
    """ST08-06 faults.py imports no YAML parser (fault plans are JSON only, R-40)."""
    tree = ast.parse(Path(faults.__file__).read_text(encoding="utf-8"))
    imported = [
        alias.name if isinstance(node, ast.Import) else f"{node.module}"
        for node in ast.walk(tree)
        if isinstance(node, ast.Import | ast.ImportFrom)
        for alias in node.names
    ]
    assert imported
    assert not [name for name in imported if "yaml" in name.lower()]


def test_st08_06_unset_env_ignores_plan(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST08-06 a valid plan with HERNESS_ENV unset is ignored with resilience.faults.ignored."""
    path = tmp_path / "plan.json"
    path.write_text(json.dumps(_VALID), encoding="utf-8")
    _enable(monkeypatch, path)
    monkeypatch.delenv("HERNESS_ENV")
    with structlog.testing.capture_logs() as logs:
        fault_point("llm.call")
    assert [log["event"] for log in logs] == ["resilience.faults.ignored"]
    assert logs[0]["env"] == "unset"
    assert reset_process_state.fault_plan is None
    assert reset_process_state.faults_enabled is False


def test_st08_06_valid_plan_under_test_sets_flag(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST08-06 a valid plan under HERNESS_ENV=test sets process_state().faults_enabled.

    Status `FAULTS ENABLED` and the degraded health() are U08-92's (later card).
    """
    path = tmp_path / "plan.json"
    path.write_text(json.dumps([{"point": "llm.call", "action": "delay:1"}]), encoding="utf-8")
    _enable(monkeypatch, path)
    fault_point("llm.call")
    assert reset_process_state.faults_enabled is True
