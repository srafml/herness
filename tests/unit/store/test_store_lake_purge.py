"""Tests for herness.store.lake_purge (U02-20 … U02-23): record purge and retention."""

import datetime
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.store.lake import LakeWriter
from herness.store.lake_purge import (
    PURGE_BATCH_MAX,
    LakePurgeResult,
    LakeRetentionResult,
    purge_partitions_before,
    purge_record_ids,
)

pytestmark = pytest.mark.unit

UTC = datetime.UTC
WHEN = datetime.datetime(2026, 9, 1, tzinfo=UTC)


def make_batch(keys: list[str], *, source: str = "jira", entity: str = "issue") -> pa.RecordBatch:
    n = len(keys)
    return pa.RecordBatch.from_pydict(
        {
            "_record_id": pa.array([f"{source}:{entity}:{k}" for k in keys], pa.string()),
            "_source": pa.array([source] * n, pa.string()),
            "_entity": pa.array([entity] * n, pa.string()),
            "_source_key": pa.array(keys, pa.string()),
            "_source_updated_at": pa.array([WHEN] * n, pa.timestamp("us", "UTC")),
            "_fetched_at": pa.array([WHEN] * n, pa.timestamp("us", "UTC")),
            "_deleted": pa.array([False] * n, pa.bool_()),
            "_payload": pa.array(["{}"] * n, pa.string()),
        }
    )


def write_file(root: Path, keys: list[str], *, source: str = "jira", entity: str = "issue") -> Path:
    """Commit one batch through LakeWriter; returns its single part file."""
    with LakeWriter(source, entity, root=root) as w:
        w.write(make_batch(keys, source=source, entity=entity))
        result = w.commit()
    return result.files[0]


def test_ut02_14_rewrite_matching_file_others_untouched(tmp_path: Path) -> None:
    """UT02-14 three files, one containing the id: it is rewritten, others byte-identical."""
    root = tmp_path / "raw"
    target = write_file(root, ["target", "keep1"])
    other_b = write_file(root, ["other2"])
    other_c = write_file(root, ["other3"])
    before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (other_b, other_c)}

    result = purge_record_ids(["jira:issue:target"], root=root)

    assert result == LakePurgeResult(
        files_scanned=3, files_rewritten=1, files_deleted=0, rows_removed=1
    )
    for path, (mtime, data) in before.items():
        assert path.stat().st_mtime_ns == mtime
        assert path.read_bytes() == data
    assert pq.read_table(target).column("_source_key").to_pylist() == ["keep1"]


def test_ut02_15_delete_emptied_file(tmp_path: Path) -> None:
    """UT02-15 file whose only row is the id: it is deleted, not rewritten."""
    root = tmp_path / "raw"
    only = write_file(root, ["target"])

    result = purge_record_ids(["jira:issue:target"], root=root)

    assert result == LakePurgeResult(
        files_scanned=1, files_rewritten=0, files_deleted=1, rows_removed=1
    )
    assert not only.exists()


def test_ut02_14_no_match_leaves_file_untouched(tmp_path: Path) -> None:
    """A file with no matching id is scanned but neither rewritten nor deleted."""
    root = tmp_path / "raw"
    path = write_file(root, ["keep1", "keep2"])
    before = (path.stat().st_mtime_ns, path.read_bytes())

    result = purge_record_ids(["jira:issue:absent"], root=root)

    assert result == LakePurgeResult(
        files_scanned=1, files_rewritten=0, files_deleted=0, rows_removed=0
    )
    assert (path.stat().st_mtime_ns, path.read_bytes()) == before


def test_ut02_14_multiple_source_entity_groups(tmp_path: Path) -> None:
    """Ids across different (source, entity) pairs are each purged in their own directory."""
    root = tmp_path / "raw"
    write_file(root, ["a"], source="jira", entity="issue")
    write_file(root, ["b"], source="jira", entity="comment")
    ids = ["jira:issue:a", "jira:comment:b"]

    result = purge_record_ids(ids, root=root)

    assert result == LakePurgeResult(
        files_scanned=2, files_rewritten=0, files_deleted=2, rows_removed=2
    )


def test_ut02_14_missing_entity_directory_is_a_no_op(tmp_path: Path) -> None:
    """An id whose (source, entity) has no lake directory contributes zero counts."""
    root = tmp_path / "raw"
    result = purge_record_ids(["jira:issue:missing"], root=root)
    assert result == LakePurgeResult(0, 0, 0, 0)


@pytest.mark.parametrize("record_ids", [[], ["x" * 10] * (PURGE_BATCH_MAX + 1)])
def test_ut02_14_count_out_of_range(tmp_path: Path, record_ids: list[str]) -> None:
    """0 ids or more than PURGE_BATCH_MAX ids -> ConfigError before any filesystem access."""
    with pytest.raises(ConfigError):
        purge_record_ids(record_ids, root=tmp_path / "raw")


@pytest.mark.parametrize("bad_id", ["no-colons", "one:two", "Jira:issue:k0", "jira:issue:"])
def test_ut02_14_invalid_record_id_format(tmp_path: Path, bad_id: str) -> None:
    """Malformed ids or invalid source/entity names -> ConfigError, value never echoed."""
    with pytest.raises(ConfigError) as info:
        purge_record_ids([bad_id], root=tmp_path / "raw")
    assert bad_id not in str(info.value)


def test_ft02_01_replace_failure_leaves_original_and_no_temp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT02-01 os.replace raises PermissionError: StoreBusy, original intact, no temp left."""
    from herness.store import lake_purge  # noqa: PLC0415

    root = tmp_path / "raw"
    target = write_file(root, ["target", "keep1"])
    original = target.read_bytes()

    def busy_replace(_src: Path, _dst: Path) -> None:
        raise PermissionError(13, "locked")

    monkeypatch.setattr(lake_purge.os, "replace", busy_replace)
    with pytest.raises(StoreBusy):
        purge_record_ids(["jira:issue:target"], root=root)

    assert target.read_bytes() == original
    assert list(target.parent.glob(".*")) == []


def test_ut02_14_unlink_non_busy_error_is_schema_violation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-busy OSError on unlink maps to SchemaViolation, not StoreBusy."""
    root = tmp_path / "raw"
    write_file(root, ["target"])
    real_unlink = Path.unlink

    def boom(self: Path, missing_ok: bool = False) -> None:
        if self.name.startswith("part-"):
            raise OSError(28, "No space left on device")
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", boom)
    with pytest.raises(SchemaViolation):
        purge_record_ids(["jira:issue:target"], root=root)


def _partition(root: Path, source: str, entity: str, day: str, *, size: int = 8) -> Path:
    part = root / source / entity / f"dt={day}"
    part.mkdir(parents=True, exist_ok=True)
    (part / "part-x.parquet").write_bytes(b"x" * size)
    return part


def test_ut02_16_deletes_before_cutoff_skips_unparsable(tmp_path: Path) -> None:
    """UT02-16 dt=2020-01-01 deleted, dt=2026-01-01 kept, dt=bad skipped as unparsable."""
    root = tmp_path / "raw"
    old = _partition(root, "jira", "issue", "2020-01-01")
    kept = _partition(root, "jira", "issue", "2026-01-01")
    bad = root / "jira" / "issue" / "dt=bad"
    bad.mkdir(parents=True)

    result = purge_partitions_before(
        datetime.date(2023, 1, 1), today=datetime.date(2026, 9, 25), root=root
    )

    assert result == LakeRetentionResult(
        partitions_deleted=1, bytes_freed=8, skipped_unparsable=1, deferred_locked=0
    )
    assert not old.exists()
    assert kept.exists()
    assert bad.exists()


def test_ut02_16_cutoff_within_30_days_of_today_raises_config_error(tmp_path: Path) -> None:
    """UT02-16 a cutoff less than 30 days before today -> ConfigError, nothing deleted."""
    root = tmp_path / "raw"
    old = _partition(root, "jira", "issue", "2020-01-01")
    with pytest.raises(ConfigError):
        purge_partitions_before(
            datetime.date(2023, 1, 1), today=datetime.date(2023, 1, 20), root=root
        )
    assert old.exists()


def test_ut02_16_cutoff_exactly_30_days_before_today_is_allowed(tmp_path: Path) -> None:
    """The boundary cutoff == today - 30 days does not raise."""
    root = tmp_path / "raw"
    today = datetime.date(2026, 9, 25)
    cutoff = today - datetime.timedelta(days=30)
    result = purge_partitions_before(cutoff, today=today, root=root)
    assert result == LakeRetentionResult(0, 0, 0, 0)


def test_ut02_16_invalid_calendar_date_is_unparsable(tmp_path: Path) -> None:
    """A dt= name with the right shape but an invalid calendar date is unparsable, not a crash."""
    root = tmp_path / "raw"
    bad = root / "jira" / "issue" / "dt=2026-13-40"
    bad.mkdir(parents=True)
    result = purge_partitions_before(
        datetime.date(2023, 1, 1), today=datetime.date(2026, 9, 25), root=root
    )
    assert result == LakeRetentionResult(0, 0, 1, 0)
    assert bad.exists()


def test_ut02_16_locked_partition_is_deferred(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A PermissionError from rmtree defers the partition instead of raising."""
    from herness.store import lake_purge  # noqa: PLC0415

    root = tmp_path / "raw"
    locked = _partition(root, "jira", "issue", "2020-01-01")

    def boom(_path: Path) -> None:
        raise PermissionError(13, "locked")

    monkeypatch.setattr(lake_purge.shutil, "rmtree", boom)
    result = purge_partitions_before(
        datetime.date(2023, 1, 1), today=datetime.date(2026, 9, 25), root=root
    )
    assert result == LakeRetentionResult(0, 0, 0, 1)
    assert locked.exists()


def test_ut02_16_containment_check_skips_escaped_partition(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The containment check (TH02-01) is defensive: a path resolving outside root is skipped."""
    root = tmp_path / "raw"
    old = _partition(root, "jira", "issue", "2020-01-01")
    monkeypatch.setattr(Path, "is_relative_to", lambda self, other: False)
    result = purge_partitions_before(
        datetime.date(2023, 1, 1), today=datetime.date(2026, 9, 25), root=root
    )
    assert result == LakeRetentionResult(0, 0, 0, 0)
    assert old.exists()


def test_ut02_16_missing_root_is_a_no_op(tmp_path: Path) -> None:
    """A raw root that does not exist yet yields an all-zero result."""
    result = purge_partitions_before(
        datetime.date(2023, 1, 1), today=datetime.date(2026, 9, 25), root=tmp_path / "raw"
    )
    assert result == LakeRetentionResult(0, 0, 0, 0)
