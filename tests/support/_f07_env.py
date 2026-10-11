"""The real spec 07 `MemoryStore` and a `Swarm` over the T06-22 driver (T07-24; impl 07 rows).

Shared by the IT07 rows (`tests/integration/memory/_it07_env.py` re-exports both) and the
FT07-03 child process (`tests.support._f07_children`). `memory_store` builds the U07-97 facade on
the test's migrated ops store and LanceDB in tmp, with the deterministic hash-seeded fake
embedder and the test redactor of `tests.unit.harness.memory._write_env` (as IT06-37 and
IT11-23 do); `swarm_for` builds a `Swarm` over a driver's deps with that memory. No side
effects at import.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tests.unit.harness.memory._write_env import PATTERNS, Env, FakeEmbed, make_writer

from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder
from herness.store.ops import core

if TYPE_CHECKING:
    from tests.integration.harness._swarm_run_env import Drv

    from herness.harness.swarm.run import Swarm

__all__ = ["Mem", "memory_store", "swarm_for"]


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


def swarm_for(drv: Drv, memory: object, **deps: Any) -> Swarm:
    """A `Swarm` over the driver's deps with `memory` in place of the fake; closed by `drv`."""
    from herness.harness.swarm.run import Swarm  # noqa: PLC0415

    swarm = Swarm(drv.hcfg.pipelines, drv.llms, memory,  # type: ignore[arg-type]
                  deps=replace(drv.deps, **deps) if deps else drv.deps)  # fmt: skip
    drv.swarms.append(swarm)
    return swarm
