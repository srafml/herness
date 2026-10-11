"""JSON fault-plan helpers of the spec 11 fault cases (impl 11 U11-50; R-40).

`write_fault_plan` validates every rule with impl 08's `FaultRule` (the `NAMED_POINTS`
registry, the action list and the `p`-needs-`seed` rule are impl 08's, never copied here) and
writes `<tmp_path>/faults.json` tmp-then-`os.replace`; plans are JSON only. `fault_env`
returns the environment a child process needs for impl 08 to honour the plan
(`HERNESS_ENV=test`; any other environment ignores the file with a WARNING).

A plain module, not a pytest plugin: it registers no fixture. The impl 08 `fault_env`
fixture of `tests.support.fault_env` (in-process plans) is a different object; import this
module as `from tests.support import faults` and call `faults.fault_env(...)`.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.core.resilience.faults import FaultRule

__all__ = ["PLAN_NAME", "fault_env", "write_fault_plan"]

PLAN_NAME: Final = "faults.json"


def _validate(index: int, rule: object) -> None:
    """One rule through impl 08's `FaultRule`; any problem -> `ConfigError` naming it."""
    if not isinstance(rule, Mapping):
        msg = f"invalid fault plan: rule {index}: not a mapping"
        raise ConfigError(msg)
    try:
        FaultRule.model_validate(dict(rule))
    except ValidationError as exc:
        first = exc.errors(include_input=False, include_url=False)[0]
        where = ".".join(str(part) for part in first["loc"]) or "rule"
        msg = f"invalid fault plan: rule {index}: {where}: {first['msg']}"
        raise ConfigError(msg) from None


def write_fault_plan(tmp_path: Path, rules: Sequence[Mapping[str, object]]) -> Path:
    """Validate `rules` (R-40) and write them to `<tmp_path>/faults.json`; return the path."""
    for index, rule in enumerate(rules):
        _validate(index, rule)
    path = tmp_path / PLAN_NAME
    tmp = tmp_path / f".{PLAN_NAME}.tmp"
    try:
        tmp.write_text(json.dumps([dict(r) for r in rules], sort_keys=True), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    return path


def fault_env(plan: Path, *, stub_services: Path | None = None) -> dict[str, str]:
    """`HERNESS_ENV=test`, `HERNESS_FAULTS=<plan>` and, when given, `HERNESS_STUB_SERVICES`."""
    env = {"HERNESS_ENV": "test", "HERNESS_FAULTS": str(plan)}
    if stub_services is not None:
        env["HERNESS_STUB_SERVICES"] = str(stub_services)
    return env
