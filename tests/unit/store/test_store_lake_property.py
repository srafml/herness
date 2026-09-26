"""Property test for herness.store.lake (PT02-01)."""

import collections
import datetime
import tempfile
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from herness.store import lake
from herness.store.lake import LakeWriter

pytestmark = pytest.mark.unit

UTC = datetime.UTC
BASE = datetime.datetime(2026, 9, 1, tzinfo=UTC)

# One batch: (rows, list of day offsets per row, has an extra column, seconds to advance).
batch_st = st.tuples(
    st.integers(min_value=0, max_value=12),
    st.lists(st.integers(min_value=0, max_value=2), min_size=12, max_size=12),
    st.booleans(),
    st.integers(min_value=0, max_value=4),
)


def _batch(n: int, start: int, days: list[int], extra: bool) -> pa.RecordBatch:
    keys = [f"k{start + i}" for i in range(n)]
    fetched = [BASE + datetime.timedelta(days=days[i], hours=i) for i in range(n)]
    cols: dict[str, pa.Array] = {
        "_record_id": pa.array([f"jira:issue:{k}" for k in keys], pa.string()),
        "_source": pa.array(["jira"] * n, pa.string()),
        "_entity": pa.array(["issue"] * n, pa.string()),
        "_source_key": pa.array(keys, pa.string()),
        "_source_updated_at": pa.array(fetched, pa.timestamp("ms", "UTC")),
        "_fetched_at": pa.array(fetched, pa.timestamp("us", "UTC")),
        "_deleted": pa.array([i % 5 == 4 for i in range(n)], pa.bool_()),
        "_payload": pa.array([None if i % 5 == 4 else "{}" for i in range(n)], pa.string()),
    }
    if extra:
        cols["extra_col"] = pa.array(list(range(n)), pa.int64())
    return pa.RecordBatch.from_pydict(cols)


@settings(max_examples=60, deadline=None)
@given(
    batches=st.lists(batch_st, max_size=8),
    buffer_rows=st.integers(min_value=1, max_value=16),
    max_open_s=st.integers(min_value=1, max_value=5),
)
def test_pt02_01_committed_ids_equal_input(
    batches: list[tuple[int, list[int], bool, int]], buffer_rows: int, max_open_s: int
) -> None:
    """PT02-01 committed _record_id multiset equals input; each file one dt, one schema."""
    now = [0.0]
    expected: collections.Counter[str] = collections.Counter()
    with tempfile.TemporaryDirectory() as tmp, pytest.MonkeyPatch.context() as mp:
        mp.setattr(lake, "BUFFER_ROWS", buffer_rows)
        root = Path(tmp) / "raw"
        writer = LakeWriter("jira", "issue", root=root, max_open_s=max_open_s, clock=lambda: now[0])
        start = 0
        for n, days, extra, advance in batches:
            batch = _batch(n, start, days, extra)
            expected.update(batch.column("_record_id").to_pylist())
            writer.write(batch)
            start += n
            now[0] += advance
        result = writer.commit()
        got: collections.Counter[str] = collections.Counter()
        for path in result.files:
            table = pq.read_table(path)
            got.update(table.column("_record_id").to_pylist())
            days_in_file = {v.date() for v in table.column("_fetched_at").to_pylist()}
            assert [f"dt={d.isoformat()}" for d in days_in_file] == [path.parent.name]
            assert table.schema.names[:8] == [name for name, _, _ in lake.META_COLUMNS]
        assert got == expected
        assert result.rows == sum(expected.values())
        assert sorted(p.name for p in root.rglob("*") if p.is_file()) == sorted(
            p.name for p in result.files
        )
