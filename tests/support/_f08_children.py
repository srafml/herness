"""Child processes of the impl 08 fault suite (T08-24: FT08-06, FT08-07, FT08-09).

Run as ``python -m tests.support._f08_children <command> ...`` from the repository root. The
fault plan reaches a child only through its environment (``HERNESS_ENV=test`` and
``HERNESS_FAULTS``, R-40); a ``kill`` rule ends the process at the fault point, so the result
line ``F08_RESULT <json>`` is printed only when no kill fired.

* ``claim <host>``: the worker bootstrap (``HERNESS_TEST_WORKER_*``), then one ``claim`` as
  ``<host>:<pid>:cpu0`` (FT08-07, ``job.after_claim``).
* ``swarm <cfg_root> <data_root>``: the T06-22 driver set-up (`_f08_swarm.build_drv`) and one
  standard ``funding_review`` run (FT08-06, ``swarm.after_finding_write``).
* ``gpu <state_file> <target>``: the worker bootstrap, the shared `fake_gpu` state, then a
  ``GpuController`` that detects the loaded class and swaps to ``target`` (FT08-09,
  ``gpu.after_stop``).
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

RESULT_PREFIX: Final = "F08_RESULT "
_USAGE: Final = (
    "usage: python -m tests.support._f08_children claim <host> | swarm <cfg_root> <data_root>"
    " | gpu <state_file> <target>\n"
)


def _claim(host: str) -> dict[str, object]:
    from tests.support.worker_bootstrap import bootstrap  # noqa: PLC0415 - child only

    from herness.core.jobs.queue import claim  # noqa: PLC0415 - child process only

    bootstrap()
    row = claim(owner=f"{host}:{os.getpid()}:cpu0", allowed_classes=["none"])
    return {"job_id": None if row is None else row.job_id}


def _swarm(cfg_root: Path, data_root: Path) -> dict[str, object]:
    import pytest  # noqa: PLC0415 - child process only
    from tests.support._f08_swarm import build_drv  # noqa: PLC0415 - child process only

    with pytest.MonkeyPatch.context() as mp:
        drv = build_drv(mp, cfg_root, data_root)
        res = asyncio.run(drv.swarm().start(drv.request(depth="standard")))
    return {"run_id": res.run_id, "status": res.status}


def _gpu(state_file: Path, target: str) -> dict[str, object]:
    from tests.support.fake_gpu import attach_shared  # noqa: PLC0415 - child process only
    from tests.support.worker_bootstrap import bootstrap  # noqa: PLC0415 - child only

    from herness.core.jobs import gpu  # noqa: PLC0415 - child process only
    from herness.core.jobs.gpu import GpuController  # noqa: PLC0415 - child process only

    bootstrap()
    fake = attach_shared(state_file, setattr)
    setattr(gpu, "_gpu", lambda: fake.gpu)  # noqa: B010 - as the parent's monkeypatch does
    ctl = GpuController(worker_id=None, runner=fake.runner(), http=fake.http)
    loaded = ctl.detect_loaded_class()
    ctl.swap(target, reason="in_job")  # type: ignore[arg-type]  # a configured class name
    return {"detected": loaded, "loaded": ctl.loaded}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["claim"] and len(args) == 2:
        out = _claim(args[1])
    elif args[:1] == ["swarm"] and len(args) == 3:
        out = _swarm(Path(args[1]), Path(args[2]))
    elif args[:1] == ["gpu"] and len(args) == 3:
        out = _gpu(Path(args[1]), args[2])
    else:
        sys.stderr.write(_USAGE)
        return 2
    sys.stdout.write(RESULT_PREFIX + json.dumps(out, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
