"""Shared set-up of the impl 07 integration acceptance rows (T07-24; IT07-01 … IT07-10).

Not a test module; no side effects at import. `memory_store` builds the real spec 07
`MemoryStore` facade (U07-97) on the test's migrated ops store and LanceDB in tmp, with the
deterministic hash-seeded fake embedder and the test redactor of the memory unit helpers
(`tests.unit.harness.memory._write_env`), as IT06-37 and IT11-23 do. `pin_tiny` copies the
`tiny_build` warehouse and `CURRENT` under the test's data root and pins the T06-22 run-driver
set-up (`drv`) to its build id (the `tests.integration.eval._eval_it_env` pattern).
"""

from __future__ import annotations

import asyncio
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.unit.harness.memory._write_env import PATTERNS, Env, FakeEmbed, make_writer

from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store.ops import core

if TYPE_CHECKING:
    import pytest
    from tests.integration.harness._swarm_run_env import Drv
    from tests.support.builds import BuildHandle

    from herness.harness.swarm.lifecycle import RunResult
    from herness.harness.swarm.run import Swarm

__all__ = [
    "Mem", "memory_store", "pin_tiny", "record_portfolio", "run_review", "run_summaries",
    "swarm_for",
]  # fmt: skip


@dataclass
class Mem:
    """The real facade and the fakes under it (embedder calls, vectors, redactor)."""

    store: MemoryStore
    env: Env

    @property
    def embed(self) -> FakeEmbed:
        return self.env.embed


def memory_store(
    tmp_path: Path, data_root: Path, *, allowed: Sequence[str] = (r"(INC|CHG|PRB)\d+",),
    cfg: MemoryConfig | None = None, llms: Any = None,
) -> Mem:  # fmt: skip
    """The U07-97 facade on `core.connection` (the test's ops store) and LanceDB in tmp."""
    cfg = cfg or MemoryConfig(injection_patterns=PATTERNS)
    env = make_writer(tmp_path / "memory", cfg=cfg)
    store = MemoryStore(
        cfg, conn_factory=core.connection, vectors=env.vectors,
        embedder=Embedder(env.embed, model_name="bge-m3"), redactor=env.redactor, llms=llms,
        allowed_numeral_patterns=allowed, data_root=data_root,
    )  # fmt: skip
    return Mem(store, env)


def record_portfolio() -> None:
    """The portfolio evidence row the scripted Writer's recommendation cites (IT06-37)."""
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415

    from herness.store.ops import record_evidence  # noqa: PLC0415

    portfolio = e._evidence("SELECT 1 AS expected_impact_usd", {}, {"expected_impact_usd": 1200})
    assert record_evidence(portfolio.model_copy(update={"query_id": e.PORTFOLIO_QID}))


def swarm_for(drv: Drv, memory: object, **deps: Any) -> Swarm:
    """A `Swarm` over the driver's deps with `memory` in place of the fake; closed by `drv`."""
    import dataclasses  # noqa: PLC0415

    from herness.harness.swarm.run import Swarm  # noqa: PLC0415

    swarm = Swarm(drv.hcfg.pipelines, drv.llms, memory,  # type: ignore[arg-type]
                  deps=dataclasses.replace(drv.deps, **deps) if deps else drv.deps)  # fmt: skip
    drv.swarms.append(swarm)
    return swarm


def run_review(drv: Drv, memory: object, kind: str = "funding_review",
               depth: str = "standard", **request: Any) -> RunResult:  # fmt: skip
    """One review run to its end through the real driver; the result."""
    swarm = swarm_for(drv, memory)
    return asyncio.run(swarm.start(drv.request(kind=kind, depth=depth, **request)))


def run_summaries(run_id: str) -> list[dict[str, Any]]:
    """The `run_summary` items of `run_id` (provenance.run_id)."""
    rows = core.read_all(
        "SELECT memory_id, content, data FROM memory_item WHERE kind = 'run_summary'"
        " AND json_extract(provenance, '$.run_id') = ?", (run_id,),
    )  # fmt: skip
    return [dict(r) for r in rows]


def pin_tiny(drv: Drv, data_root: Path, build: BuildHandle, monkeypatch: pytest.MonkeyPatch
             ) -> Path:  # fmt: skip
    """Copy the tiny warehouse and `CURRENT` under `data_root`, pin the driver to its id."""
    from tests.integration.harness import _swarm_run_env as e  # noqa: PLC0415
    from tests.unit.harness._blackboard_env import FakeWarehouse  # noqa: PLC0415

    wh = data_root / "warehouse"
    wh.mkdir(exist_ok=True)
    copy = wh / build.warehouse.name
    shutil.copyfile(build.warehouse, copy)
    (wh / "CURRENT").write_text(build.build_id, encoding="ascii")
    monkeypatch.setattr(e, "BUILD_ID", build.build_id)
    drv.pool.warehouse = FakeWarehouse(build.build_id)
    return copy
