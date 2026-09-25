"""Tests for herness.store.lake (U02-08 … U02-19): contract, writer, rotation, commit, abort."""

import datetime
import os
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from structlog.testing import capture_logs

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.store import lake
from herness.store.errors import LakeContractError, LakeStateError
from herness.store.lake import META_COLUMNS, LakeFileSet, LakeWriter

pytestmark = pytest.mark.unit

UTC = datetime.UTC
DAY1 = datetime.datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
DAY2 = datetime.datetime(2026, 9, 2, 3, 0, tzinfo=UTC)
META_NAMES = [name for name, _, _ in META_COLUMNS]


@pytest.fixture
def lake_root(tmp_path: Path) -> Path:
    """Temp raw lake root (spec 11 fixture name)."""
    return tmp_path / "raw"


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def make_batch(  # noqa: PLR0913 - test builder with independent knobs
    n: int,
    *,
    start: int = 0,
    source: str = "jira",
    entity: str = "issue",
    fetched: datetime.datetime | list[datetime.datetime] = DAY1,
    extra: dict[str, pa.Array] | None = None,
    payload: list[str | None] | None = None,
) -> pa.RecordBatch:
    keys = [f"k{start + i}" for i in range(n)]
    fetched_list = fetched if isinstance(fetched, list) else [fetched] * n
    cols: dict[str, pa.Array] = {
        "_record_id": pa.array([f"{source}:{entity}:{k}" for k in keys], pa.string()),
        "_source": pa.array([source] * n, pa.string()),
        "_entity": pa.array([entity] * n, pa.string()),
        "_source_key": pa.array(keys, pa.string()),
        "_source_updated_at": pa.array(
            [DAY1 - datetime.timedelta(minutes=i) for i in range(n)], pa.timestamp("us", "UTC")
        ),
        "_fetched_at": pa.array(fetched_list, pa.timestamp("us", "UTC")),
        "_deleted": pa.array([False] * n, pa.bool_()),
        "_payload": pa.array(payload if payload is not None else ["{}"] * n, pa.string()),
    }
    cols.update(extra or {})
    return pa.RecordBatch.from_pydict(cols)


def with_column(batch: pa.RecordBatch, name: str, array: pa.Array) -> pa.RecordBatch:
    idx = batch.schema.get_field_index(name)
    return batch.set_column(idx, name, array)


def committed_files(root: Path) -> list[Path]:
    return sorted(root.rglob("part-*.parquet"))


def temp_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob(".*") if p.is_file())


@pytest.fixture
def writer(lake_root: Path) -> Iterator[LakeWriter]:
    w = LakeWriter("jira", "issue", root=lake_root)
    yield w
    w.abort()


def test_ut02_01_write_commit_one_file(lake_root: Path) -> None:
    """UT02-01 10 rows one day: one part file under dt=, rows, max, metadata first."""
    batch = make_batch(10, extra={"summary_len": pa.array(range(10), pa.int64())})
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(batch)
    result = w.commit()
    files = committed_files(lake_root)
    assert len(files) == 1
    assert files[0].parent == lake_root.resolve() / "jira" / "issue" / "dt=2026-09-01"
    assert result == LakeFileSet(files=(files[0],), rows=10, max_source_updated_at=DAY1)
    table = pq.read_table(files[0])
    assert table.column_names == [*META_NAMES, "summary_len"]
    for name, dtype, nullable in META_COLUMNS:
        field = table.schema.field(name)
        assert field.type == dtype
        assert field.nullable is nullable
    assert table.num_rows == 10
    assert temp_files(lake_root) == []


def test_ut02_01_metadata_reordered_and_normalised(writer: LakeWriter) -> None:
    """UT02-01 metadata columns first, entity order kept, large_string and ns cast."""
    base = make_batch(3)
    cols = {"zeta": pa.array([1, 2, 3]), "alpha": pa.array(["a", "b", "c"])}
    for name in reversed(META_NAMES):
        cols[name] = base.column(name)
    cols["_source"] = base.column("_source").cast(pa.large_string())
    cols["_fetched_at"] = base.column("_fetched_at").cast(pa.timestamp("ns", "UTC"))
    out = lake._validate_batch(pa.RecordBatch.from_pydict(cols), "jira", "issue")
    assert out.schema.names == [*META_NAMES, "zeta", "alpha"]
    assert out.schema.field("_source").type == pa.string()
    assert out.schema.field("_fetched_at").type == pa.timestamp("us", "UTC")


def test_ut02_02_missing_column(writer: LakeWriter) -> None:
    """UT02-02 batch without _deleted -> missing_column."""
    batch = make_batch(4)
    batch = batch.drop_columns(["_deleted"])
    with pytest.raises(LakeContractError) as info:
        writer.write(batch)
    assert (info.value.rule, info.value.column) == ("missing_column", "_deleted")


def test_ut02_03_record_id_format(writer: LakeWriter) -> None:
    """UT02-03 _record_id not the concatenation -> record_id_format with bad row count."""
    batch = make_batch(5)
    ids = ["jira:issue:k0", "x", "jira:issue:k2", "jira:bug:k3", "jira:issue:k4"]
    batch = with_column(batch, "_record_id", pa.array(ids, pa.string()))
    with pytest.raises(LakeContractError) as info:
        writer.write(batch)
    assert (info.value.rule, info.value.column, info.value.bad_rows) == (
        "record_id_format",
        "_record_id",
        2,
    )
    assert "jira:bug" not in str(info.value)


def test_ut02_04_source_mismatch(writer: LakeWriter) -> None:
    """UT02-04 _source differs from the writer source -> source_mismatch."""
    with pytest.raises(LakeContractError) as info:
        writer.write(make_batch(3, source="servicenow"))
    assert (info.value.rule, info.value.column, info.value.bad_rows) == (
        "source_mismatch",
        "_source",
        3,
    )


def test_ut02_04_entity_mismatch(writer: LakeWriter) -> None:
    """UT02-04 _entity differs from the writer entity -> entity_mismatch."""
    with pytest.raises(LakeContractError) as info:
        writer.write(make_batch(2, entity="bug"))
    assert info.value.rule == "entity_mismatch"


def test_ut02_05_payload_null(writer: LakeWriter) -> None:
    """UT02-05 non-tombstone row with NULL _payload -> payload_null; tombstones may be NULL."""
    batch = make_batch(3, payload=[None, "{}", None])
    batch = with_column(batch, "_deleted", pa.array([True, False, False]))
    with pytest.raises(LakeContractError) as info:
        writer.write(batch)
    assert (info.value.rule, info.value.column, info.value.bad_rows) == (
        "payload_null",
        "_payload",
        1,
    )


def test_ut02_05_naive_timestamp_type(writer: LakeWriter) -> None:
    """UT02-05 naive timestamp column -> type."""
    batch = make_batch(2)
    naive = batch.column("_fetched_at").cast(pa.timestamp("us"))
    with pytest.raises(LakeContractError) as info:
        writer.write(with_column(batch, "_fetched_at", naive))
    assert (info.value.rule, info.value.column) == ("type", "_fetched_at")


@pytest.mark.parametrize(
    ("column", "array", "rule"),
    [
        ("_source_key", pa.array([1, 2], pa.int64()), "type"),
        ("_deleted", pa.array([0, 1], pa.int8()), "type"),
        ("_source_key", pa.array(["k0", None], pa.string()), "null"),
    ],
)
def test_ut02_05_other_type_and_null_rules(
    writer: LakeWriter, column: str, array: pa.Array, rule: str
) -> None:
    """UT02-05 wrong metadata types and NULLs in non-nullable columns are rejected."""
    with pytest.raises(LakeContractError) as info:
        writer.write(with_column(make_batch(2), column, array))
    assert (info.value.rule, info.value.column) == (rule, column)


def test_ut02_05_column_name_and_duplicate(writer: LakeWriter) -> None:
    """UT02-05 entity column names must match COLUMN_RE and be unique."""
    bad_name = make_batch(1, extra={"Summary": pa.array([1])})
    with pytest.raises(LakeContractError) as info:
        writer.write(bad_name)
    assert (info.value.rule, info.value.column) == ("column_name", "Summary")
    dup = make_batch(1).append_column("_deleted", pa.array([False]))
    with pytest.raises(LakeContractError) as info:
        writer.write(dup)
    assert (info.value.rule, info.value.column) == ("duplicate_column", "_deleted")


def test_ut02_05_timestamp_overflow_is_type_error(writer: LakeWriter) -> None:
    """UT02-05 a time value outside the microsecond range fails the type rule."""
    batch = make_batch(1)
    far = pa.array([2**62], pa.int64()).cast(pa.timestamp("s", "UTC"))
    with pytest.raises(LakeContractError) as info:
        writer.write(with_column(batch, "_source_updated_at", far))
    assert (info.value.rule, info.value.column) == ("type", "_source_updated_at")


def _random_payloads(n: int) -> list[str | None]:
    raw = os.urandom(16 * n).hex()
    return [raw[i * 32 : (i + 1) * 32] for i in range(n)]


def test_ut02_06_size_rotation(lake_root: Path) -> None:
    """UT02-06 target_bytes = 1 MiB, >5 MiB of rows -> >=4 files, each >=1 MiB except the last."""
    w = LakeWriter("jira", "issue", root=lake_root, target_bytes=2**20)
    rows = lake.BUFFER_ROWS
    with capture_logs() as logs:
        for i in range(4):
            w.write(make_batch(rows, start=i * rows, payload=_random_payloads(rows)))
        w.write(make_batch(100, start=4 * rows, payload=_random_payloads(100)))
        result = w.commit()
    sizes = [f.stat().st_size for f in result.files]
    assert len(result.files) >= 4
    assert result.rows == 4 * rows + 100
    by_name = sorted(result.files, key=lambda p: p.name)
    assert all(f.stat().st_size >= 2**20 for f in by_name[:-1])
    assert sum(sizes) >= 5 * 2**20
    reasons = [e["reason"] for e in logs if e["event"] == "store.lake.file_rotated"]
    assert reasons.count("size") >= 3


def test_ut02_07_age_rotation(lake_root: Path) -> None:
    """UT02-07 fake clock, max_open_s = 10: write, advance 11 s, write -> two files, age."""
    clock = FakeClock()
    w = LakeWriter("jira", "issue", root=lake_root, max_open_s=10, clock=clock)
    rows = lake.BUFFER_ROWS
    with capture_logs() as logs:
        w.write(make_batch(rows))
        clock.now += 11
        w.write(make_batch(5, start=rows))
        result = w.commit()
    assert len(result.files) == 2
    assert result.rows == rows + 5
    rotated = [e for e in logs if e["event"] == "store.lake.file_rotated"]
    assert [e["reason"] for e in rotated] == ["age", "commit"]
    assert rotated[0]["rows"] == rows
    assert rotated[0]["log_level"] == "debug"


def test_ut02_08_batch_spanning_two_dates(lake_root: Path) -> None:
    """UT02-08 one batch over two _fetched_at dates -> two partitions, split counts."""
    fetched = [DAY1, DAY2, DAY1, DAY2, DAY2]
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(5, fetched=fetched))
    result = w.commit()
    counts = {f.parent.name: pq.read_metadata(f).num_rows for f in result.files}
    assert counts == {"dt=2026-09-01": 2, "dt=2026-09-02": 3}
    assert result.files == tuple(sorted(result.files, key=lambda p: p.as_posix()))


def test_ut02_08_dt_change_after_flush_closes_file(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-08 a new dt after a flushed-but-open file never lands in the old partition."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 2)
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(2))
    w.write(make_batch(1, start=2, fetched=DAY2))
    result = w.commit()
    for f in result.files:
        dates = {v.date().isoformat() for v in pq.read_table(f).column("_fetched_at").to_pylist()}
        assert {f"dt={d}" for d in dates} == {f.parent.name}


def test_ut02_09_schema_change(lake_root: Path) -> None:
    """UT02-09 second batch with an extra column -> two files, each with one schema."""
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(3))
    w.write(make_batch(2, start=3, extra={"priority": pa.array(["p1", "p2"])}))
    result = w.commit()
    assert len(result.files) == 2
    names = sorted(len(pq.read_schema(f).names) for f in result.files)
    assert names == [len(META_NAMES), len(META_NAMES) + 1]


def test_ut02_10_abort_then_write(lake_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-10 abort removes temps; later write and commit raise LakeStateError."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 2)
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(3))
    assert temp_files(lake_root)
    w.abort()
    w.abort()
    assert temp_files(lake_root) == []
    assert committed_files(lake_root) == []
    with pytest.raises(LakeStateError) as info:
        w.write(make_batch(1))
    assert (info.value.state, info.value.method) == ("aborted", "write")
    with pytest.raises(LakeStateError):
        w.commit()


def test_ut02_10_abort_leftover_logged(lake_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-10 a temp that cannot be unlinked is counted and logged, never raised."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(1))

    def locked(self: Path, missing_ok: bool = False) -> None:
        raise PermissionError(13, "locked")

    monkeypatch.setattr(Path, "unlink", locked)
    with capture_logs() as logs:
        w.abort()
    leftover = [e for e in logs if e["event"] == "store.lake.abort_leftover"]
    assert [(e["count"], e["log_level"]) for e in leftover] == [(1, "warning")]
    monkeypatch.undo()
    for temp in temp_files(lake_root):
        temp.unlink()


def test_ut02_10_commit_after_commit(writer: LakeWriter) -> None:
    """UT02-10 committed writer rejects write and commit; abort is a no-op."""
    writer.write(make_batch(1))
    writer.commit()
    writer.abort()
    with pytest.raises(LakeStateError) as info:
        writer.write(make_batch(1))
    assert info.value.state == "committed"


def test_ut02_11_commit_without_writes(lake_root: Path) -> None:
    """UT02-11 no writes -> empty LakeFileSet, no files, no directories."""
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(0))
    assert w.commit() == LakeFileSet(files=(), rows=0, max_source_updated_at=None)
    assert not lake_root.exists()


def test_ut02_11_commit_retry_is_idempotent(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-11 a sharing violation during commit raises StoreBusy; the retry completes."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    w = LakeWriter("jira", "issue", root=lake_root)
    w.write(make_batch(2, fetched=[DAY1, DAY2]))
    real_replace = os.replace
    calls: list[int] = []

    def flaky(src: Path, dst: Path) -> None:
        calls.append(1)
        if len(calls) == 2:
            err = OSError(13, "sharing violation")
            raise err
        real_replace(src, dst)

    monkeypatch.setattr(lake.os, "replace", flaky)
    with pytest.raises(StoreBusy):
        w.commit()
    result = w.commit()
    assert result.rows == 2
    assert len(result.files) == 2
    assert temp_files(lake_root) == []


def test_ut02_11_other_os_error_is_schema_violation(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-11 a non-sharing OSError maps to SchemaViolation naming only the errno."""

    def boom(self: Path, **kwargs: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    monkeypatch.setattr(Path, "mkdir", boom)
    w = LakeWriter("jira", "issue", root=lake_root)
    with pytest.raises(SchemaViolation) as info:
        w.write(make_batch(1))
    assert str(info.value) == "lake write failed for jira/issue: ENOSPC"
    assert not isinstance(info.value, LakeContractError)


def test_ut02_11_windows_sharing_violation_is_store_busy(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-11 winerror 32 maps to StoreBusy."""
    err = OSError(0, "used by another process")
    err.winerror = 32  # type: ignore[attr-defined]

    def busy(self: Path, **kwargs: object) -> None:
        raise err

    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    monkeypatch.setattr(Path, "mkdir", busy)
    w = LakeWriter("jira", "issue", root=lake_root)
    with pytest.raises(StoreBusy):
        w.write(make_batch(1))


def test_ut02_12_context_manager_exception(
    lake_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT02-12 exception inside with: temps deleted, exception propagates."""
    monkeypatch.setattr(lake, "BUFFER_ROWS", 1)
    seen: list[Path] = []

    def body() -> None:
        with LakeWriter("jira", "issue", root=lake_root) as w:
            w.write(make_batch(2))
            seen.extend(temp_files(lake_root))
            msg = "boom"
            raise RuntimeError(msg)

    with pytest.raises(RuntimeError, match="boom"):
        body()
    assert len(seen) == 1
    assert temp_files(lake_root) == []
    assert committed_files(lake_root) == []


def test_ut02_12_context_manager_uncommitted_and_committed(lake_root: Path) -> None:
    """UT02-12 normal exit without commit aborts with a warning; after commit it is silent."""
    with capture_logs() as logs, LakeWriter("jira", "issue", root=lake_root) as w:
        w.write(make_batch(1))
    assert [e["event"] for e in logs] == ["store.lake.uncommitted_exit"]
    with pytest.raises(LakeStateError):
        w.commit()
    with capture_logs() as logs, LakeWriter("jira", "issue", root=lake_root) as w2:
        w2.write(make_batch(1))
        w2.commit()
    assert "store.lake.uncommitted_exit" not in [e["event"] for e in logs]
    assert len(committed_files(lake_root)) == 1


@pytest.mark.parametrize("name", ["Incident", "a/b", "..", "c:"])
def test_ut02_13_invalid_names(lake_root: Path, name: str) -> None:
    """UT02-13 Incident, a/b, .., c: -> ConfigError, the value is not echoed."""
    with pytest.raises(ConfigError) as info:
        lake.validate_name("source", name)
    assert str(info.value) == f"invalid lake source name (length {len(name)})"
    with pytest.raises(ConfigError):
        lake.partition_dir(lake_root, "jira", name, DAY1.date())


def test_ut02_13_valid_name_and_partition(lake_root: Path) -> None:
    """UT02-13 ok_name accepted; partition dir and glob are canonical."""
    assert lake.validate_name("entity", "ok_name") == "ok_name"
    part = lake.partition_dir(lake_root, "jira", "ok_name", DAY1.date())
    assert part == lake_root / "jira" / "ok_name" / "dt=2026-09-01"
    glob = lake.lake_glob(lake_root, "jira", "ok_name")
    assert glob == f"{lake_root.as_posix()}/jira/ok_name/**/[!.]*.parquet"


def test_ut02_13_partition_containment(lake_root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-13 containment check rejects a path outside raw_root even past validation."""
    monkeypatch.setattr(lake, "validate_name", lambda kind, value: value)
    with pytest.raises(ConfigError, match="outside the raw root"):
        lake.partition_dir(lake_root, "..", "..", DAY1.date())


@pytest.mark.parametrize(
    "kwargs",
    [
        {"target_bytes": 2**20 - 1},
        {"target_bytes": 2**30 + 1},
        {"max_open_s": 0},
        {"max_open_s": 86_401},
    ],
)
def test_ut02_13_limits_out_of_range(lake_root: Path, kwargs: dict[str, int]) -> None:
    """UT02-13 LakeWriter limits outside their ranges raise ConfigError."""
    with pytest.raises(ConfigError):
        LakeWriter("jira", "issue", root=lake_root, **kwargs)  # type: ignore[arg-type]


def test_ut02_13_default_root_from_layout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-13 root=None resolves to data_layout().raw."""
    from herness.store.layout import DataLayout  # noqa: PLC0415

    layout = DataLayout.from_root(tmp_path / "data")
    monkeypatch.setattr(lake, "data_layout", lambda: layout)
    w = LakeWriter("jira", "issue")
    w.write(make_batch(1))
    result = w.commit()
    assert result.files[0].is_relative_to(layout.raw)


@pytest.mark.parametrize(("source", "entity"), [("..", "x"), ("a", "b/../../c"), ("C:", "x")])
def test_st02_01_path_traversal_names(tmp_path: Path, source: str, entity: str) -> None:
    """ST02-01 traversal and drive-letter names -> ConfigError; nothing created."""
    root = tmp_path / "raw"
    with pytest.raises(ConfigError):
        LakeWriter(source, entity, root=root)
    assert list(tmp_path.iterdir()) == []
