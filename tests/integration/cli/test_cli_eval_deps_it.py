"""The real eval composition root across jobs in one process (T09-24 review I1, U09-98).

``_eval_deps.make_deps`` runs unpatched (real ``WarehousePool``, ``Verifier``, ``Swarm``,
``ChatService`` and model registry) on a copy of ``tiny_build`` under the ``ops_store`` data
root; only the memory store is the recording fake (the ``local`` profile's real store needs the
off-network fallback client, T09-27 note 6). A second job must open the same build and add no
writer thread; a new config object must close the old handles so the rebuilt pool can harden
its own connection to the same build.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.support.builds import BuildHandle
from tests.support.cli_env import CliEnv
from tests.support.fake_memory import FakeMemoryStore
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval._runner_env import FakeCtx

from herness._cli import _eval_deps
from herness.core import config as herness_config
from herness.harness import memory
from herness.store.ops.resilience import bind_core_backends

pytestmark = pytest.mark.integration


@pytest.fixture
def build_id(
    cli_env: CliEnv, ops_store: OpsStoreHandle, tiny_build: BuildHandle,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[str]:  # fmt: skip
    """tiny_build's warehouse and CURRENT under the data root; the cli_env config loaded."""
    wh = ops_store.data_root / "warehouse"
    wh.mkdir(exist_ok=True)
    shutil.copyfile(tiny_build.warehouse, wh / tiny_build.warehouse.name)
    (wh / "CURRENT").write_text(tiny_build.build_id, encoding="ascii")
    herness_config.init_config("hybrid", (), cli_env.write_config())  # egress on (T09-27 note 6)
    bind_core_backends()
    monkeypatch.setattr(memory, "get_memory_store", FakeMemoryStore)
    monkeypatch.setattr(_eval_deps._Cache, "shared", None)
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    yield tiny_build.build_id
    held = _eval_deps._Cache.shared
    if held is not None:
        held.close()


def _query(deps: object, build_id: str) -> int:
    cursor = deps.warehouse_for(build_id)  # type: ignore[attr-defined]
    row = cursor.execute("SELECT count(*) FROM meta.build").fetchone()
    return int(row[0])


def test_cv_t09_24_real_make_deps_twice_reuses_handles_and_threads(build_id: str) -> None:
    """CV-T09-24 two eval jobs in one process: the second opens the same build through the
    shared pool and builds no second swarm (no extra writer thread); after a config reload the
    old swarm and handles are closed and a fresh pool opens the build again."""
    ctx = FakeCtx({"profile": "local", "repeat": 1})
    first = _eval_deps.make_deps(ctx)  # type: ignore[arg-type]
    assert _query(first, build_id) >= 1
    swarm, chat = first.swarm_for("hybrid"), first.chat_for("hybrid")
    threads = threading.active_count()
    second = _eval_deps.make_deps(FakeCtx({"profile": "local", "repeat": 1}))  # type: ignore[arg-type]
    assert _query(second, build_id) >= 1
    assert second.swarm_for("hybrid") is swarm
    assert second.chat_for("hybrid") is chat
    assert threading.active_count() <= threads
    herness_config.init_config("hybrid", (), Path("config"))  # a new config object (cwd = root)
    third = _eval_deps.make_deps(ctx)  # type: ignore[arg-type]
    assert _query(third, build_id) >= 1  # hardening on a fresh pool succeeds
    assert third.swarm_for("hybrid") is not swarm
