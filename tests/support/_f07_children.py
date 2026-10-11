"""Child process of the impl 07 fault row FT07-03 (T07-24; the T11-33 `_f11_children` pattern).

Run as ``python -m tests.support._f07_children recs <cfg_root> <data_root> <memory_root>
<mode>`` from the repository root. The child rebuilds the T06-22 run-driver set-up
(`tests.support._f08_swarm.build_drv`: scripted agents, stub pipeline, fake Verifier) on the
parent's config tree and migrated ops store, with the real spec 07 `MemoryStore` (LanceDB under
``<memory_root>``), and starts one standard funding review. Its record step calls
`MemoryStore.write_recommendations`, and the process dies in that call:

* ``plan``: the fault plan in the environment (`tests.support.faults.fault_env`, R-40) kills
  the process at the `sqlite.write` point of the `write_recommendations` transaction.
* ``hold``: after the recommendation rows are inserted inside the open ``BEGIN IMMEDIATE``
  transaction, the child prints ``F07_IN_TX <run_id>`` and blocks on stdin until the parent
  kills it (an OS-level kill inside the transaction).

The line ``F07_SURVIVED <run_id>`` is printed only when the child was not killed.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any, Final

IN_TX: Final = "F07_IN_TX "
SURVIVED: Final = "F07_SURVIVED "
MODES: Final = ("plan", "hold")
_USAGE: Final = (
    "usage: python -m tests.support._f07_children recs <cfg_root> <data_root> <memory_root>"
    " plan|hold\n"
)


def _hold_inside_transaction(mp: Any) -> None:
    """After the rows are inserted (transaction open), announce it and wait to be killed."""
    from herness.store import ops  # noqa: PLC0415 - child process only

    insert = ops.insert_recommendations

    def held(rows: Sequence[Any], **kwargs: Any) -> None:
        insert(rows, **kwargs)
        sys.stdout.write(IN_TX + str(rows[0]["run_id"]) + "\n")
        sys.stdout.flush()
        sys.stdin.readline()  # the parent kills the process; it never writes

    mp.setattr(ops, "insert_recommendations", held)


def run_recs(cfg_root: Path, data_root: Path, memory_root: Path, mode: str) -> str:
    """Start one funding review through the real driver and the real `MemoryStore`."""
    import asyncio  # noqa: PLC0415 - child process only

    import pytest  # noqa: PLC0415 - child process only
    from tests.integration.memory._it07_env import (  # noqa: PLC0415 - child process only
        memory_store,
        swarm_for,
    )
    from tests.support._f08_swarm import build_drv  # noqa: PLC0415 - child process only

    with pytest.MonkeyPatch.context() as mp:
        drv = build_drv(mp, cfg_root, data_root)
        if mode == "hold":
            _hold_inside_transaction(mp)
        mem = memory_store(memory_root, data_root)
        try:
            res = asyncio.run(swarm_for(drv, mem.store).start(drv.request(depth="standard")))
        finally:
            for swarm in drv.swarms:
                swarm.close()
        return res.run_id


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the survival line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 5 or args[0] != "recs" or args[4] not in MODES:
        sys.stderr.write(_USAGE)
        return 2
    run_id = run_recs(Path(args[1]), Path(args[2]), Path(args[3]), args[4])
    sys.stdout.write(SURVIVED + run_id + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
