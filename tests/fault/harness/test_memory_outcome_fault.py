"""Fault test FT07-04 for the outcome job (impl 07 §11; U07-86, U07-87, T07-18).

`error:QueryError` is injected through the spec 08 fault plan (`fault_env`) at the outcome
series query (`fault_point("sql.query", kind="outcome_measure")`): the job fails, nothing is
written for the failing pair, and the next sweep measures the same pair exactly once.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.fault_env import FaultEnv
from tests.support.ops_store import OpsStoreHandle
from tests.unit.harness.memory._outcome_env import (
    OutcomeEnv,
    accepted_rec,
    flat,
    improves,
    make_env,
    outcomes,
    plant,
    run,
    summaries,
)

from herness.core.errors import QueryError
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.fault

PEERS = ("svc_p1", "svc_p2", "svc_p3", "svc_p4")
_RULE = {"point": "sql.query", "action": "error:QueryError", "kind": "outcome_measure"}


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
) -> OutcomeEnv:
    del ops_store, reset_process_state
    hours = {"svc_t": improves(), "svc_u": improves()} | dict.fromkeys(PEERS, flat)
    return make_env(tmp_path, monkeypatch, plant(hours))


def test_ft07_04_query_error_fails_job_next_sweep_measures_once(
    env: OutcomeEnv, fault_env: FaultEnv
) -> None:
    """FT07-04 `error:QueryError` on the outcome query: the job fails with QueryError and
    writes no outcome or summary; the next sweep measures the same pair once; a third sweep
    measures nothing."""
    rec_id = accepted_rec("svc_t")
    fault_env([_RULE | {"count": 1}])
    with pytest.raises(QueryError):
        run()
    assert outcomes() == []
    assert summaries() == []
    assert env.opened == 1  # the connection was opened and closed by the failed job
    first, _ = run()
    assert first.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    second, _ = run()
    assert second.result["measured"] == 0
    assert [(o["rec_id"], o["measurement"]) for o in outcomes()] == [(rec_id, 1)]
    assert len(summaries()) == 1


def test_ft07_04_failure_at_second_pair_keeps_first_and_retries_second_once(
    env: OutcomeEnv, fault_env: FaultEnv
) -> None:
    """FT07-04 the job fails at the first failing pair: the earlier pair keeps its outcome,
    and the retried sweep measures only the failed pair, once."""
    accepted_rec("svc_t")
    accepted_rec("svc_u")
    fault_env([_RULE | {"nth": 2}])
    with pytest.raises(QueryError):
        run()
    assert len(outcomes()) == 1
    retried, _ = run()
    assert retried.result == {"measured": 1, "skipped": 0, "verdicts": {"paid_off": 1}}
    assert len(outcomes()) == 2
    assert len(summaries()) == 2
