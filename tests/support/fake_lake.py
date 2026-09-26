"""tests.support.fake_lake: a recording stand-in for `herness.store.lake.LakeWriter` (T01-06).

`FakeLake` is a writer factory with the `(source, entity) -> LakeWriter` shape the sync
runner takes. Every writer it builds appends to one shared ordered `events` list
(`("open", n)`, `("write", n, rows)`, `("commit", n, rows)`, `("abort", n)`), so tests can
assert the exact order of writer calls against other recorded calls such as
`set_watermark`. `commit()` returns a real `LakeFileSet` with one fake path per call under
`root/<source>/<entity>/` (nothing is written to disk), the committed row count and the
maximum `_source_updated_at` of the written batches, like the real writer.
"""

from __future__ import annotations

import datetime
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pyarrow as pa
import pyarrow.compute as pc

from herness.store.lake import LakeFileSet, LakeWriter

type Event = tuple[object, ...]


class FakeLakeWriter:
    """One recorded writer; `write`/`commit`/`abort` follow the real state rules."""

    def __init__(self, lake: FakeLake, number: int, source: str, entity: str) -> None:
        self.lake, self.number, self.source, self.entity = lake, number, source, entity
        self.batches: list[pa.RecordBatch] = []
        self.state = "open"
        self.abort_calls = 0

    def write(self, batch: pa.RecordBatch) -> None:
        """Record the batch (validation is the real writer's job, tested in spec 02)."""
        assert self.state == "open", f"write on {self.state} writer"
        self.batches.append(batch)
        self.lake.events.append(("write", self.number, batch.num_rows))

    def commit(self) -> LakeFileSet:
        """Record the commit and return a `LakeFileSet` for the buffered rows."""
        assert self.state == "open", f"commit on {self.state} writer"
        if self.lake.fail_on_commit is not None:
            raise self.lake.fail_on_commit
        self.state = "committed"
        rows = sum(b.num_rows for b in self.batches)
        self.lake.events.append(("commit", self.number, rows))
        files: tuple[Path, ...] = ()
        if rows:
            name = f"part-{self.number:04d}.parquet"
            files = (self.lake.root / self.source / self.entity / name,)
        return LakeFileSet(files=files, rows=rows, max_source_updated_at=self._max_updated())

    def abort(self) -> None:
        """Record the abort; a no-op after commit, like the real writer."""
        self.abort_calls += 1
        self.lake.events.append(("abort", self.number))
        if self.lake.abort_error is not None:
            raise self.lake.abort_error
        if self.state != "committed":
            self.state = "aborted"

    def _max_updated(self) -> datetime.datetime | None:
        values = [pc.max(b["_source_updated_at"]).as_py() for b in self.batches if b.num_rows]
        present = [v for v in values if v is not None]
        return max(present) if present else None


@dataclass
class FakeLake:
    """Writer factory recording every writer and call in order."""

    root: Path
    events: list[Event] = field(default_factory=list)
    writers: list[FakeLakeWriter] = field(default_factory=list)
    fail_on_commit: BaseException | None = None
    abort_error: BaseException | None = None

    def __call__(self, source: str, entity: str) -> FakeLakeWriter:
        writer = FakeLakeWriter(self, len(self.writers), source, entity)
        self.writers.append(writer)
        self.events.append(("open", writer.number))
        return writer

    def factory(self) -> Callable[[str, str], LakeWriter]:
        """This fake typed as the runner's `writer_factory` parameter."""
        return cast("Callable[[str, str], LakeWriter]", self)

    def kinds(self) -> list[str]:
        """Event kinds in order, e.g. `["open", "write", "commit"]`."""
        return [str(e[0]) for e in self.events]
