"""Child process of the memory write crash test (impl 07 FT07-01; the T05-27 `loop_kill` pattern).

Run as ``python -m tests.support.memory_kill <ops_db> <vectors_dir> <content>`` from the
repository root with ``HERNESS_ENV=test`` and ``HERNESS_FAULTS`` naming a plan with a ``kill``
rule on ``sqlite.write`` (kind ``memory_embedding_flag``). The child builds the real
`MemoryStore` on the parent's migrated ops store, with a LanceDB index whose upsert fails (the
vector write after the SQLite commit) and the deterministic test embedder, and proposes one
human glossary item: the item commits, the vector write fails, and the process is killed by
the fault plan before `embedding_pending` is recorded. If it survives it prints one line
``MEMORY_KILL_SURVIVED <memory_id>`` and exits 0.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Final

SURVIVED: Final = "MEMORY_KILL_SURVIVED "
_USAGE: Final = "usage: python -m tests.support.memory_kill <ops_db> <vectors_dir> <content>\n"


def run(argv: Sequence[str]) -> str:
    """Propose one item through the facade; returns its id when the process was not killed."""
    db_path, vectors_dir, content = Path(argv[0]), Path(argv[1]), argv[2]
    from tests.unit.harness.memory._write_env import (  # noqa: PLC0415 - child process only
        PATTERNS,
        FakeEmbed,
        make_writer,
        proposal,
    )

    from herness.core.errors import ModelUnavailable  # noqa: PLC0415 - child process only
    from herness.harness.memory import MemoryStore  # noqa: PLC0415 - child process only
    from herness.harness.memory.settings import MemoryConfig  # noqa: PLC0415 - child only
    from herness.harness.memory.store import Embedder, VectorIndex  # noqa: PLC0415
    from herness.store.ops import core  # noqa: PLC0415 - child process only
    from herness.store.vectors import VectorStore  # noqa: PLC0415 - child process only

    class _UpsertDown(VectorIndex):
        def upsert(self, rows: Sequence[object]) -> None:  # type: ignore[override]
            msg = "memory vector store unavailable: upsert"
            raise ModelUnavailable(msg)

    core.reset_connections(path=db_path)
    vectors = _UpsertDown(lambda: VectorStore(vectors_dir))
    embed = FakeEmbed()
    env = make_writer(vectors_dir.parent, vectors=vectors, embed=embed)
    store = MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=vectors, embedder=Embedder(embed, model_name="bge-m3"), redactor=env.redactor,
        llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",), data_root=vectors_dir.parent,
    )  # fmt: skip
    return store.propose(proposal(content)).memory_id


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: exit 0 after printing the survival line, 2 on a usage error."""
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 3:
        sys.stderr.write(_USAGE)
        return 2
    sys.stdout.write(SURVIVED + run(args) + "\n")
    return 0


if __name__ == "__main__":  # pragma: no cover - child process entry
    raise SystemExit(main())
