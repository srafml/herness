"""Child process of the ServiceNow crash tests (impl 01 FT01-01, FT01-03; T01-25).

Run as ``python -m tests.fault.connectors._sn_child <config_dir> <ops_db> <start> <end>``
from the repository root. Like ``tests.support.sync_kill`` it loads the parent's config tree,
binds the parent's migrated ops store and runs the `sync` job handler for ServiceNow, here in
backfill mode over ``[start, end)`` (ISO dates). The table API is the seeded
``incident_table`` cassette served in-process (the ``_servicenow_env`` mock pool transport,
no socket). The fault plan comes only from ``HERNESS_ENV`` and ``HERNESS_FAULTS``; a ``kill``
rule ends the process before the result line is printed.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

RESULT_PREFIX: Final = "SN_RESULT "
_USAGE: Final = "usage: python -m tests.fault.connectors._sn_child <cfg_dir> <ops_db> <from> <to>\n"


def run(config_dir: Path, db_path: Path, start: str, end: str) -> dict[str, object]:
    """Install the ServiceNow stand-in and run the backfill `sync` job; the outcome."""
    import keyring  # noqa: PLC0415 - child process only
    import pytest  # noqa: PLC0415 - child process only
    from tests.support.build_harness import FakeJobContext  # noqa: PLC0415 - child only
    from tests.support.fake_keyring import MemoryKeyring  # noqa: PLC0415 - child only
    from tests.support.sn_cassettes import FakeServiceNow  # noqa: PLC0415 - child only
    from tests.unit.connectors import _servicenow_env as env  # noqa: PLC0415 - child only
    from tests.unit.connectors._http_data import bind_resilience  # noqa: PLC0415 - child only

    from herness.connectors.jobs import handle_sync  # noqa: PLC0415 - child process only
    from herness.core.config import init_config  # noqa: PLC0415 - child process only
    from herness.store.ops import reset_connections  # noqa: PLC0415 - child process only

    keyring.set_keyring(MemoryKeyring())
    env.store_secret()
    init_config("local", config_dir=config_dir, env={})
    reset_connections(path=db_path)
    bind_resilience()
    host = env.SnHost(FakeServiceNow.from_cassette("incident_table"))
    env.install(pytest.MonkeyPatch(), host)  # a process-long patch: the child just exits
    payload = {"source": "servicenow", "mode": "backfill", "start": start, "end": end}
    outcome = handle_sync(FakeJobContext(payload, kind="sync"))
    return {"status": outcome.status, "result": outcome.result}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 4:
        sys.stderr.write(_USAGE)
        return 2
    outcome = run(Path(args[0]), Path(args[1]), args[2], args[3])
    sys.stdout.write(RESULT_PREFIX + json.dumps(outcome, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
