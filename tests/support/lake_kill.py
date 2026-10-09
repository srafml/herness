"""Child process of the lake crash test (impl 02 FT02-06; T02-22).

Run as ``python -m tests.support.lake_kill <raw_root> <source_dir> <source> <entity> [kill]``
from the repository root. It reads every parquet file of ``<source_dir>/<source>/<entity>``
(a committed lake layout) and writes the same rows again through a `LakeWriter` rooted at
``raw_root``: three batches whose ``_fetched_at`` falls on three different days, so the commit
renames three files. With ``kill`` the process ends itself right after the first successful
``os.replace`` of the commit (Windows ``TerminateProcess`` exit 15, POSIX SIGKILL): one final
file, the others still temps. Without it the commit completes and one JSON line
``LAKE_RESULT {...}`` is printed.
"""

from __future__ import annotations

import datetime
import json
import os
import signal
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

import pyarrow as pa
import pyarrow.parquet as pq

RESULT_PREFIX: Final = "LAKE_RESULT "
DAYS: Final = (datetime.date(2024, 3, 2), datetime.date(2024, 3, 3), datetime.date(2024, 3, 4))
_USAGE: Final = (
    "usage: python -m tests.support.lake_kill <raw_root> <source_dir> <source> <entity> [kill]\n"
)


def _batches(table: pa.Table) -> list[pa.RecordBatch]:
    """One batch per day in `DAYS`: the same rows with `_fetched_at` moved to that day."""
    index = table.schema.get_field_index("_fetched_at")
    out: list[pa.RecordBatch] = []
    for day in DAYS:
        stamp = datetime.datetime(day.year, day.month, day.day, 12, tzinfo=datetime.UTC)
        column = pa.array([stamp] * table.num_rows, type=table.schema.field(index).type)
        out.append(table.set_column(index, "_fetched_at", column).combine_chunks().to_batches()[0])
    return out


def _die_after_first_rename() -> None:
    """Patch `os.replace` (used by `LakeWriter.commit`): the process dies after the first rename."""
    real = os.replace
    done: list[str] = []

    def replace(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
        real(src, dst)
        done.append(str(dst))
        if len(done) == 1:
            sys.stderr.write("lake_kill.after_first_rename\n")
            sys.stderr.flush()
            os.kill(os.getpid(), getattr(signal, "SIGKILL", signal.SIGTERM))

    os.replace = replace  # `herness.store.lake` calls `os.replace` through the module


def run(
    raw_root: Path, source_dir: Path, source: str, entity: str, *, kill: bool
) -> dict[str, int]:
    """Re-write the entity's rows with a `LakeWriter` at `raw_root`; the file set summary."""
    from herness.store.lake import LakeWriter  # noqa: PLC0415 - child process only

    files = sorted((source_dir / source / entity).glob("dt=*/part-*.parquet"))
    table = pa.concat_tables([pq.read_table(f) for f in files])
    if kill:
        _die_after_first_rename()
    with LakeWriter(source, entity, root=raw_root) as writer:
        for batch in _batches(table):
            writer.write(batch)
        done = writer.commit()
    return {"files": len(done.files), "rows": done.rows}


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the result line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) not in (4, 5) or (len(args) == 5 and args[4] != "kill"):
        sys.stderr.write(_USAGE)
        return 2
    outcome = run(Path(args[0]), Path(args[1]), args[2], args[3], kill=len(args) == 5)
    sys.stdout.write(RESULT_PREFIX + json.dumps(outcome, sort_keys=True) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
