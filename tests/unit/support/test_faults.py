"""Tests for tests.support.faults (impl 11 U11-50, T11-33): UT11-77, UT11-115.

`write_fault_plan` validates every rule through impl 08's `FaultRule` (the `NAMED_POINTS`
registry and the action list, R-40) and writes `<tmp>/faults.json`; `fault_env` returns the
environment that makes impl 08 honour the plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.support import faults

from herness.core.errors import ConfigError
from herness.core.resilience.faults import load_fault_plan

pytestmark = pytest.mark.unit

BAD: Any = ["not", "a", "mapping"]
GOOD = {"point": "job.before_complete", "action": "kill", "kind": "review", "nth": 1}


def test_ut11_77_unknown_point_task_before_commit_is_config_error(tmp_path: Path) -> None:
    """UT11-77 `write_fault_plan` with the typo point `task.before_commit` -> `ConfigError`;
    nothing is written."""
    with pytest.raises(ConfigError, match=r"task\.before_commit"):
        faults.write_fault_plan(tmp_path, [{"point": "task.before_commit", "action": "kill"}])
    assert list(tmp_path.iterdir()) == []


def test_ut11_77_unknown_action_and_non_mapping_rule_are_config_errors(tmp_path: Path) -> None:
    """UT11-77 an action outside the impl 08 list and a rule that is not a mapping are
    `ConfigError` too (the same validation path)."""
    with pytest.raises(ConfigError, match="rule 0"):
        faults.write_fault_plan(tmp_path, [{"point": "llm.call", "action": "explode"}])
    with pytest.raises(ConfigError, match="rule 1"):
        faults.write_fault_plan(tmp_path, [GOOD, BAD])
    assert list(tmp_path.iterdir()) == []


def test_ut11_115_valid_plan_written_as_faults_json(tmp_path: Path) -> None:
    """UT11-115 a valid `job.before_complete` rule: the file is `faults.json`, `json.loads`
    returns the rules, and impl 08 loads it."""
    rules: list[dict[str, object]] = [
        GOOD,
        {"point": "sql.query", "action": "error:QueryError", "role": "analyst"},
    ]
    path = faults.write_fault_plan(tmp_path, rules)
    assert path == tmp_path / "faults.json"
    assert json.loads(path.read_text(encoding="utf-8")) == rules
    assert path.read_text(encoding="utf-8") == json.dumps(rules, sort_keys=True)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["faults.json"]  # tmp replaced
    assert [r.point for r in load_fault_plan(path).rules] == ["job.before_complete", "sql.query"]


def test_ut11_115_p_without_seed_is_config_error(tmp_path: Path) -> None:
    """UT11-115 a rule using `p` without `seed` -> `ConfigError`; with `seed` it is valid."""
    with pytest.raises(ConfigError, match="p requires seed"):
        faults.write_fault_plan(tmp_path, [{"point": "sql.query", "action": "timeout", "p": 0.2}])
    assert not (tmp_path / "faults.json").exists()
    path = faults.write_fault_plan(
        tmp_path, [{"point": "sql.query", "action": "timeout", "p": 0.2, "seed": 7}]
    )
    assert json.loads(path.read_text(encoding="utf-8"))[0]["seed"] == 7


def test_ut11_115_fault_env_holds_test_env_and_plan(tmp_path: Path) -> None:
    """UT11-115 `fault_env`: `HERNESS_ENV=test` and `HERNESS_FAULTS=<plan>` (R-40), plus
    `HERNESS_STUB_SERVICES` only when given."""
    plan = faults.write_fault_plan(tmp_path, [GOOD])
    assert faults.fault_env(plan) == {"HERNESS_ENV": "test", "HERNESS_FAULTS": str(plan)}
    stubs = tmp_path / "stubs.json"
    assert faults.fault_env(plan, stub_services=stubs) == {
        "HERNESS_ENV": "test", "HERNESS_FAULTS": str(plan), "HERNESS_STUB_SERVICES": str(stubs),
    }  # fmt: skip
