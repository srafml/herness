"""Child process of the scoring crash test (impl 04 FT04-01 metrics part; T04-13).

Run as ``python -m tests.support.scoring_kill <build.duckdb> <state.json> [block]`` from the
repository root. The child points scoring at the `metrics_tiny` config (small catalog), opens
the build file writable and runs `run_scoring` with a job context whose checkpoint state lives
in ``state.json`` (so it survives the process). With ``block`` the `check` step, after running
its checks inside the step transaction, prints `BLOCKED_LINE` and sleeps until the parent
terminates the process (an OS-level kill, no fault point: impl 08's registry is unchanged,
R-40). Without it the child prints one ``SCORING_RESULT <report json>`` line and exits 0.
"""

from __future__ import annotations

import json
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import duckdb
from pydantic import JsonValue
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_scoring import patches, small_catalog
from tests.support.metrics_tiny import BUILD_ID

from herness.metrics import scoring
from herness.metrics.context import StepContext, StepResult

BLOCKED_LINE: Final = "SCORING_BLOCKED_IN_CHECK"
RESULT_PREFIX: Final = "SCORING_RESULT "
_USAGE: Final = "usage: python -m tests.support.scoring_kill <build.duckdb> <state.json> [block]\n"
_BLOCK_S: Final = 600.0


class FileJobContext(FakeJobContext):
    """`FakeJobContext` whose job state is a JSON file (the ops store in a real worker)."""

    def __init__(self, path: Path) -> None:
        text = path.read_text(encoding="utf-8") if path.exists() else "{}"
        super().__init__(state=json.loads(text))
        self._path = path

    def save_state(self, state: dict[str, JsonValue]) -> None:
        super().save_state(state)
        self._path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")


def _blocking_check(con: duckdb.DuckDBPyConnection, sc: StepContext) -> StepResult:
    """The real check step, then hold the open transaction until the process is killed."""
    scoring.run_check_step(con, sc)
    sys.stdout.write(BLOCKED_LINE + "\n")
    sys.stdout.flush()
    time.sleep(_BLOCK_S)
    msg = "scoring_kill child was not terminated"
    raise RuntimeError(msg)


def run(db_path: Path, state_path: Path, *, block: bool) -> dict[str, object]:
    """Run scoring on the build file; the report as a JSON-ready dict."""
    for obj, name, value in patches(small_catalog()):
        setattr(obj, name, value)
    if block:
        scoring._STEP_FUNCS["check"] = _blocking_check
    con = duckdb.connect(str(db_path))
    try:
        report = scoring.run_scoring(BUILD_ID, con=con, ctx=FileJobContext(state_path))
    finally:
        con.close()
    return report.model_dump(mode="json")


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3) or args[2:] not in ([], ["block"]):
        sys.stderr.write(_USAGE)
        return 2
    report = run(Path(args[0]), Path(args[1]), block=args[2:] == ["block"])
    sys.stdout.write(RESULT_PREFIX + json.dumps(report, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
