"""Child process of the promotion crash test (impl 07 FT07-01, T07-28; `memory_kill` pattern).

Run as ``python -m tests.support.memory_promote_kill <ops_db> <data_dir> <run_id>`` from the
repository root with ``HERNESS_ENV=test`` and ``HERNESS_FAULTS`` naming a plan with a ``kill``
rule (``nth`` 2) on ``sqlite.write`` (kind ``memory_promote``). The run holds two fingerprints:
the first fingerprint's transaction commits (its template and qa_pair rows, both
`embedding_pending`), and the process is killed at the second one, before the single batched
vector flush of `promote_procedural` ran. If it survives it prints ``PROMOTE_KILL_SURVIVED``
and exits 0.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

SURVIVED: Final = "PROMOTE_KILL_SURVIVED"
_USAGE: Final = "usage: python -m tests.support.memory_promote_kill <ops_db> <data_dir> <run_id>\n"


def run(argv: Sequence[str]) -> int:
    """Promote the run; returns the number of templates created when not killed."""
    db_path, data_dir, run_id = Path(argv[0]), Path(argv[1]), argv[2]
    from tests.unit.harness.memory._procedural_env import (  # noqa: PLC0415 - child only
        LOW,
        make_deps,
    )
    from tests.unit.harness.memory._write_env import NOW  # noqa: PLC0415 - child process only

    from herness.harness.memory.procedural import promote_procedural  # noqa: PLC0415
    from herness.store.ops import core  # noqa: PLC0415 - child process only

    core.reset_connections(path=db_path)
    pe = make_deps(data_dir, LOW)
    return promote_procedural(run_id, deps=pe.deps, now=NOW).templates_created


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the survival line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        sys.stderr.write(_USAGE)
        return 2
    run(args)
    sys.stdout.write(SURVIVED + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
