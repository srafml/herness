"""Security test for herness.store.lake with DuckDB (ST02-02, OI-06)."""

import datetime
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest

from herness.store import lake
from herness.store.lake import LakeWriter

pytestmark = pytest.mark.integration

DAY = datetime.datetime(2026, 9, 1, 12, tzinfo=datetime.UTC)


def _batch(n: int) -> pa.RecordBatch:
    keys = [f"k{i}" for i in range(n)]
    return pa.RecordBatch.from_pydict(
        {
            "_record_id": pa.array([f"jira:issue:{k}" for k in keys]),
            "_source": pa.array(["jira"] * n),
            "_entity": pa.array(["issue"] * n),
            "_source_key": pa.array(keys),
            "_source_updated_at": pa.array([DAY] * n, pa.timestamp("us", "UTC")),
            "_fetched_at": pa.array([DAY] * n, pa.timestamp("us", "UTC")),
            "_deleted": pa.array([False] * n),
            "_payload": pa.array(["{}"] * n),
        }
    )


def _matched(glob: str) -> list[str]:
    rows = duckdb.sql("SELECT file FROM glob($g)", params={"g": glob}).fetchall()
    return [str(row[0]) for row in rows]


def test_st02_02_glob_ignores_temp_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST02-02 flushed but uncommitted rows: lake_glob matches zero files until commit."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 3)
    root = tmp_path / "raw"
    writer = LakeWriter("jira", "issue", root=root)
    writer.write(_batch(4))
    temps = [p for p in root.rglob("*") if p.is_file()]
    assert temps
    assert all(p.name.startswith(".part-") and ".parquet.tmp-" in p.name for p in temps)
    glob = lake.lake_glob(root, "jira", "issue")
    assert _matched(glob) == []
    # OI-06: a dot-prefixed name ending in .parquet must be excluded by [!.] itself.
    (temps[0].parent / ".hidden.parquet").write_bytes(b"")
    assert _matched(glob) == []
    result = writer.commit()
    matched = _matched(glob)
    assert len(matched) == len(result.files) == 1
    assert Path(matched[0]).name == result.files[0].name
    (temps[0].parent / ".hidden.parquet").unlink()
    count = duckdb.sql("SELECT count(*) FROM read_parquet($g)", params={"g": glob}).fetchone()
    assert count == (4,)
