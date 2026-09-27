"""Child process of the connector crash tests (impl 01 FT01-02, spec 11 X5; T01-13).

Run as ``python -m tests.support.sync_kill <config_dir> <ops_db> [<source>]`` from the
repository root (``source`` defaults to ``files``).
The child loads the config tree written by the parent, binds the parent's (already migrated)
ops store and runs the `sync` job handler for that source once. The fault plan comes
only from the environment (``HERNESS_ENV=test`` and ``HERNESS_FAULTS``), exactly as in a real
worker: with a `kill` rule on ``connector.before_watermark`` the process ends itself inside
the sync and never prints its result line. Without a plan it prints one JSON line
``{"status": ..., "results": [...]}`` and exits 0.

Windows has no SIGKILL: the fault hook's ``os.kill(pid, SIGTERM)`` is ``TerminateProcess``
with exit code 15; on POSIX the child dies of SIGKILL (return code -9). `KILLED_RETURNCODE`
is the value the parent expects on the running platform.
"""

from __future__ import annotations

import json
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

KILLED_RETURNCODE: Final = -signal.SIGKILL if hasattr(signal, "SIGKILL") else signal.SIGTERM
RESULT_PREFIX: Final = "SYNC_RESULT "
_USAGE: Final = "usage: python -m tests.support.sync_kill <config_dir> <ops_db> [<source>]\n"


def run_sync(config_dir: Path, db_path: Path, source: str = "files") -> dict[str, object]:
    """Load the config, bind the ops store and run `handle_sync` for `source`; the outcome."""
    import keyring  # noqa: PLC0415 - child process only
    from tests.support.build_harness import FakeJobContext  # noqa: PLC0415 - child only
    from tests.support.fake_keyring import MemoryKeyring  # noqa: PLC0415 - child only

    from herness.connectors.jobs import handle_sync  # noqa: PLC0415 - child process only
    from herness.core.config import init_config  # noqa: PLC0415 - child process only
    from herness.store.ops import reset_connections  # noqa: PLC0415 - child process only

    keyring.set_keyring(MemoryKeyring())
    init_config("local", config_dir=config_dir, env={})
    reset_connections(path=db_path)
    outcome = handle_sync(FakeJobContext({"source": source}, kind="sync"))
    return {"status": outcome.status, "results": outcome.result.get("results")}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (2, 3):
        sys.stderr.write(_USAGE)
        return 2
    outcome = run_sync(Path(args[0]), Path(args[1]), *args[2:])
    sys.stdout.write(RESULT_PREFIX + json.dumps(outcome, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
