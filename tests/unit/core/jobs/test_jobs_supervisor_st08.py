"""Security tests of the worker supervisor (impl 08 ST08-13 supervisor half; TH08-13; T08-21).

A child that sends pickled bytes, or a JSON message with an extra field, is treated as crashed:
it is terminated and its job finished with `ModelUnavailable("child_crash")`; nothing is ever
unpickled (the supervisor only runs `decode_message`: JSON plus strict pydantic validation).
The ST08-01 `gpu_request` half is in test_jobs_supervisor_gpu.py; the child half of ST08-13 is
in test_jobs_context.py.
"""

from __future__ import annotations

import json
import pickle  # noqa: TID251 - ST08-13 sends a pickle attack payload; nothing here unpickles

import pytest
from tests.unit.core.jobs._supervisor_env import FakeChild, SupEnv

from herness.core.jobs._supervisor_child import ChildRun

pytestmark = pytest.mark.unit

TRIGGERED: list[str] = []


def _mark() -> None:  # pragma: no cover - runs only if something unpickled the payload
    TRIGGERED.append("unpickled")


class _Bomb:
    def __reduce__(self) -> tuple[object, tuple[()]]:
        return (_mark, ())


def _attack(payload: bytes) -> object:
    def script(child: FakeChild) -> int:
        child.heartbeat()
        child.raw(payload)
        child.wait_released(60)
        return 0

    return script


@pytest.mark.parametrize(
    "payload",
    [
        pickle.dumps(_Bomb()),
        json.dumps({"type": "heartbeat", "note": None, "extra": 1}).encode(),
        json.dumps({"type": "stop", "reason": "cancel"}).encode(),  # parent → child only
    ],
    ids=["pickle", "extra_field", "wrong_direction"],
)
def test_st08_13_bad_frame_child_treated_as_crashed(sup_env: SupEnv, payload: bytes) -> None:
    """ST08-13 (supervisor half) pickled bytes, a JSON message with an extra field, or a
    parent → child message from the child: `SchemaViolation` in the supervisor, the child
    is terminated and its job requeued as `child_crash`; nothing is unpickled."""
    sup_env.ctx.script = _attack(payload)  # type: ignore[assignment]
    job_id = sup_env.enqueue()
    sup = sup_env.supervisor()
    assert sup.start() is None
    runs: list[ChildRun] = []

    def crashed() -> bool:
        run = sup.slots["cpu0"]
        if run is not None and run not in runs:
            runs.append(run)
        return sup_env.job(job_id).status == "queued"

    sup_env.drive(sup, crashed)
    assert runs
    assert runs[0].crashed
    assert sup_env.ctx.procs[0].terminated
    row = sup_env.job(job_id)
    assert row.last_error is not None
    assert (row.last_error["class"], row.last_error["message"]) == (
        "ModelUnavailable",
        "child_crash",
    )
    assert TRIGGERED == []
    sup.terminate_now = True
    assert sup.stop() == 0
