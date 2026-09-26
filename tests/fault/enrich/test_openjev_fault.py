"""FT03-02: a malformed decider reply for one item is retried once, then that item is an
error output while the other items succeed (impl 03 §6, U03-53; T03-12).

`StubDeciderServer` (impl 11) and `DeciderChain` (T08-10) do not exist yet: per the program
ruling the stub's `malformed_json` mode is a scripted MockNet reply for one item, and the
"reaches the next chain member" half is a carry-over (the error output is what the chain
hands on). The second test drives the same path through the impl 08 fault plan at the
`decider.batch` point (R-40, `HERNESS_ENV=test`).
"""

from __future__ import annotations

from typing import Any

import pytest
from tests.support.fault_env import FaultEnv
from tests.unit.enrich._openjev_support import JSON, QS, Reply, install, item, jev_env, ok

from herness.core.resilience import ProcessState
from herness.enrich.deciders.openjev import OpenJevDecider
from herness.enrich.settings import OpenJevSettings

pytestmark = pytest.mark.fault

__all__ = ["jev_env"]  # the fixture is used by name


def _decider() -> OpenJevDecider:
    return OpenJevDecider(OpenJevSettings(), api_key=None, image_tag="0.4.0", samples=None)


def test_ft03_02_malformed_json_for_one_item(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT03-02 stub decider malformed_json for one item: one retry, then an item error."""
    seen: dict[str, int] = {}

    def stub(body: dict[str, Any]) -> Reply:
        seen[body["state"]] = seen.get(body["state"], 0) + 1
        if body["state"] == item(2).text:
            return 200, b'{"answers": {"is_outage": {"noul": 0.9}', JSON  # truncated JSON
        return ok(body)

    install(monkeypatch, stub)
    out = _decider().decide([item(1), item(2), item(3)], QS)
    assert seen == {item(1).text: 1, item(2).text: 2, item(3).text: 1}
    assert [o.error for o in out] == [None, "OutputValidationError", None]
    assert out[1].answers == {}
    assert out[1].record_id == "INC00002"
    assert all(len(o.answers) == 3 for o in (out[0], out[2]))


def test_ft03_02_fault_plan_malformed_json_at_decider_batch(
    jev_env: ProcessState, monkeypatch: pytest.MonkeyPatch, fault_env: FaultEnv
) -> None:
    """FT03-02 fault plan `malformed_json` at decider.batch twice: item error, no request."""
    net = install(monkeypatch, ok)
    fault_env([{"point": "decider.batch", "action": "malformed_json", "count": 2}])
    out = _decider().decide([item(1)], QS)
    assert out[0].error == "OutputValidationError"
    assert net.requests == []
    again = _decider().decide([item(1)], QS)  # the plan is spent: the next call succeeds
    assert again[0].error is None
    assert len(net.requests) == 1
