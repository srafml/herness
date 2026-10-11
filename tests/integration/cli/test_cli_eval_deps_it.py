"""The real eval composition root and its per-job teardown (T09-24 review r2 ruling, U09-98).

``_eval_deps.make_deps`` and ``run_eval_job`` run unpatched (real ``WarehousePool``,
``Verifier``, ``Swarm``, ``ChatService`` and model registry) on a copy of ``tiny_build`` under
the ``ops_store`` data root; the runner's ``handle_eval`` is replaced by a job body that uses
the deps (queries the build, builds the swarm and the chat service). After the job, the same
build must open through a fresh ``WarehousePool`` built the way the review handler builds it
(``SwarmDeps.from_config``), a second eval job must run, and no writer thread may be left.
Profile ``hybrid`` (egress on): the ``local`` registry refuses its off-network fallback client
(T09-27 note 6); the memory store is the recording fake.
"""

from __future__ import annotations

import shutil
import threading
from typing import Any

import pytest
from tests.support.builds import BuildHandle
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval._runner_env import FakeCtx

from herness._cli import _eval_deps
from herness.core import config as herness_config
from herness.core.types import JobOutcome
from herness.eval import runner
from herness.harness import memory
from herness.harness.swarm.run import SwarmDeps
from herness.store.ops.resilience import bind_core_backends

pytestmark = pytest.mark.integration


@pytest.fixture
def build_id(
    cli_env: CliEnv, ops_store: OpsStoreHandle, tiny_build: BuildHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> str:  # fmt: skip
    """tiny_build's warehouse and CURRENT under the data root; the cli_env config loaded."""
    wh = ops_store.data_root / "warehouse"
    wh.mkdir(exist_ok=True)
    shutil.copyfile(tiny_build.warehouse, wh / tiny_build.warehouse.name)
    (wh / "CURRENT").write_text(tiny_build.build_id, encoding="ascii")
    herness_config.init_config("hybrid", (), cli_env.write_config())  # egress on (T09-27 note 6)
    bind_core_backends()
    monkeypatch.setattr(memory, "get_memory_store", FakeMemoryStore)
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(_eval_deps._Jobs, "open", {})
    return tiny_build.build_id


def _count(cursor: Any) -> int:
    return int(cursor.execute("SELECT count(*) FROM meta.build").fetchone()[0])


def test_cv_t09_24_eval_job_teardown_frees_the_build(
    build_id: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T09-24 an eval job through `run_eval_job` (real make_deps on tiny_build) builds and
    uses its swarm, chat service and pool; afterwards the review handler's own fresh pool opens
    the same build, a second eval job runs, and no writer thread is left behind."""
    seen: list[int] = []

    def job_body(ctx: Any) -> JobOutcome:
        deps = runner._Binding.factory(ctx)  # type: ignore[misc]
        seen.append(_count(deps.warehouse_for(build_id)))
        assert deps.swarm_for("hybrid") is deps.swarm_for("hybrid")  # one per profile
        deps.chat_for("hybrid")
        return JobOutcome(status="done", result={"run_id": "r", "passed": True})

    monkeypatch.setattr(runner._Binding, "factory", _eval_deps.make_deps)
    monkeypatch.setattr(runner, "handle_eval", job_body)
    threads = threading.active_count()
    outcome = _eval_deps.run_eval_job(FakeCtx({"profile": "hybrid", "repeat": 1}))  # type: ignore[arg-type]
    assert outcome.status == "done"
    assert threading.active_count() <= threads  # the swarm's writer thread was stopped
    assert _eval_deps._Jobs.open == {}
    review_pool = SwarmDeps.from_config(herness_config.get_config()).warehouses
    try:
        assert _count(review_pool.get(build_id).cursor()) >= 1  # hardening succeeds
    finally:
        review_pool.close_all()
    again = _eval_deps.run_eval_job(FakeCtx({"profile": "hybrid", "repeat": 1}))  # type: ignore[arg-type]
    assert again.status == "done"
    assert seen == [seen[0], seen[0]]
    assert threading.active_count() <= threads
