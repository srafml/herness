"""Child process of the planner kill/resume test (impl 06 IT06-26; the `scoring_kill` pattern).

Run as ``python -m tests.support.planner_kill <cfg_dir> <ops_db> <run_id> block|resume`` from the
repository root. The child loads the test config, binds the parent's migrated ops store and runs
`run_planning` for the run over the scripted fake LLM (`tests.support.planner_env`).

``block``: post-processing, which runs right after the judge task completed, prints
`BLOCKED_LINE` and sleeps until the parent kills the process (an OS-level kill, no fault point:
impl 08's registry is unchanged, R-40). The planner task is left `running` with its proposals in
the checkpoint and the judge task `done`.

``resume``: spec 08 `recover_run_tasks` resets the interrupted planner (F06-02 step 3), then
`run_planning` runs again; the child prints one ``PLANNER_RESULT {...}`` line with the outcome,
the model calls per role and the number of reset tasks, and exits 0.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Final

BLOCKED_LINE: Final = "PLANNER_BLOCKED_AFTER_JUDGE"
RESULT_PREFIX: Final = "PLANNER_RESULT "
_USAGE: Final = (
    "usage: python -m tests.support.planner_kill <cfg_dir> <ops_db> <run_id> block|resume\n"
)
_BLOCK_S: Final = 600.0
_MAX_TASK_ATTEMPTS: Final = 3


def _blocking[F: Callable[..., object]](real: F) -> F:
    """Signal the parent, then hold the step until the process is killed."""

    def blocked(*args: object, **kwargs: object) -> object:
        del args, kwargs
        sys.stdout.write(BLOCKED_LINE + "\n")
        sys.stdout.flush()
        time.sleep(_BLOCK_S)
        msg = "planner_kill child was not terminated"
        raise RuntimeError(msg)

    del real
    return blocked  # type: ignore[return-value]


def run(cfg_dir: Path, db_path: Path, run_id: str, mode: str) -> dict[str, object]:
    """Run the planner step once; the outcome and the model calls per role."""
    from tests.support import planner_env as pe  # noqa: PLC0415 - child process only

    from herness.core.jobs.tasks import recover_run_tasks  # noqa: PLC0415 - child only
    from herness.harness.swarm import planner  # noqa: PLC0415 - child process only
    from herness.store.ops import core  # noqa: PLC0415 - child process only

    hcfg = pe.configure(cfg_dir, setattr)
    core.reset_connections(path=db_path)
    reset = 0
    if mode == "block":
        planner.postprocess_plan = _blocking(planner.postprocess_plan)  # type: ignore[assignment]
    else:
        summary = recover_run_tasks(run_id, max_task_attempts=_MAX_TASK_ATTEMPTS)
        reset = summary.running_reset
    client = pe.CountingClient(pe.SCRIPT)
    env, bb = pe.make_env(run_id, hcfg, client, setattr)
    try:
        outcome = asyncio.run(planner.run_planning(env))
    finally:
        bb.close()
    return {"outcome": outcome, "calls": dict(client.roles), "running_reset": reset}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4 or args[3] not in {"block", "resume"}:
        sys.stderr.write(_USAGE)
        return 2
    result = run(Path(args[0]), Path(args[1]), args[2], args[3])
    sys.stdout.write(RESULT_PREFIX + json.dumps(result, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
