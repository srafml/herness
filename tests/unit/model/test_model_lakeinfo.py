"""Tests for herness.model.lakeinfo (U02-78 … U02-81): lake inventory scan."""

import datetime
from pathlib import Path

import pyarrow as pa
import pytest

from herness.core.errors import ConfigError, SchemaViolation
from herness.model import lakeinfo
from herness.model.lakeinfo import (
    EXPECTED_ENTITIES,
    EntityInventory,
    LakeInventory,
    scan_lake,
)
from herness.store.lake import LakeWriter, lake_glob
from herness.store.layout import DataLayout

pytestmark = pytest.mark.unit

WHEN = datetime.datetime(2026, 9, 1, tzinfo=datetime.UTC)
META = (
    "_record_id",
    "_source",
    "_entity",
    "_source_key",
    "_source_updated_at",
    "_fetched_at",
    "_deleted",
    "_payload",
)


def _batch(
    source: str, entity: str, keys: list[str], extra: dict[str, list[str]]
) -> pa.RecordBatch:
    n = len(keys)
    cols: dict[str, pa.Array] = {
        "_record_id": pa.array([f"{source}:{entity}:{k}" for k in keys], pa.string()),
        "_source": pa.array([source] * n, pa.string()),
        "_entity": pa.array([entity] * n, pa.string()),
        "_source_key": pa.array(keys, pa.string()),
        "_source_updated_at": pa.array([WHEN] * n, pa.timestamp("us", "UTC")),
        "_fetched_at": pa.array([WHEN] * n, pa.timestamp("us", "UTC")),
        "_deleted": pa.array([False] * n, pa.bool_()),
        "_payload": pa.array(["{}"] * n, pa.string()),
    }
    for name, values in extra.items():
        cols[name] = pa.array(values, pa.string())
    return pa.RecordBatch.from_pydict(cols)


def _commit(raw: Path, source: str, entity: str, extra: dict[str, list[str]]) -> Path:
    keys = [f"k{i}" for i in range(len(next(iter(extra.values()), ["x"])))]
    with LakeWriter(source, entity, root=raw) as writer:
        writer.write(_batch(source, entity, keys, extra))
        return writer.commit().files[0]


def test_ut02_55_expected_entities_fixed() -> None:
    """UT02-55 EXPECTED_ENTITIES lists the fixed raw entities of design 02 §3.1 in order."""
    assert EXPECTED_ENTITIES == (
        ("servicenow", "incident"),
        ("servicenow", "change_request"),
        ("servicenow", "problem"),
        ("servicenow", "cmdb_ci"),
        ("servicenow", "cmdb_ci_service"),
        ("servicenow", "cmdb_rel_ci"),
        ("servicenow", "sys_user_group"),
        ("servicenow", "cmn_department"),
        ("servicenow", "task_sla"),
        ("jira", "issue"),
        ("monitoring", "event"),
        ("monitoring", "metric_daily"),
    )
    assert isinstance(EXPECTED_ENTITIES, tuple)


def test_ut02_55_scan_union_temp_absent(tmp_path: Path) -> None:
    """UT02-55 incident files and a temp file: columns union, temp ignored, absent entity."""
    layout = DataLayout.from_root(tmp_path / "data")
    first = _commit(layout.raw, "servicenow", "incident", {"number": ["INC1"]})
    second = _commit(layout.raw, "servicenow", "incident", {"priority": ["1"], "state": ["2"]})
    temp = first.parent / ".part-x.parquet.tmp-abc"
    temp.write_bytes(b"not parquet")
    dotted = first.parent / ".hidden.parquet"
    dotted.write_bytes(b"not parquet either")

    inv = scan_lake(layout)

    assert isinstance(inv, LakeInventory)
    assert inv.root == layout.raw
    assert set(inv.entities) == set(EXPECTED_ENTITIES)
    incident = inv.get("servicenow", "incident")
    assert incident.present is True
    assert incident.files == 2
    assert incident.bytes == first.stat().st_size + second.stat().st_size
    assert incident.columns == frozenset({*META, "number", "priority", "state"})
    assert incident.glob == lake_glob(layout.raw, "servicenow", "incident")
    assert incident.from_synth is False
    problem = inv.get("servicenow", "problem")
    assert (problem.present, problem.files, problem.bytes, problem.columns) == (
        False,
        0,
        0,
        frozenset(),
    )
    assert inv.from_synth is False


def test_ut02_55_get_unknown_entity_absent(tmp_path: Path) -> None:
    """UT02-55 get() for an entity never scanned returns present=False with no columns."""
    inv = scan_lake(DataLayout.from_root(tmp_path / "data"))
    other = inv.get("files", "budget")
    assert other == EntityInventory(
        source="files",
        entity="budget",
        glob=lake_glob(inv.root, "files", "budget"),
        present=False,
        files=0,
        bytes=0,
        columns=frozenset(),
        from_synth=False,
    )
    with pytest.raises(ConfigError):
        inv.get("Bad", "x")
    synth = LakeInventory(root=inv.root, entities={}, from_synth=True)
    assert synth.get("files", "budget").from_synth is True
    with pytest.raises(TypeError):
        inv.entities[("x", "y")] = other  # type: ignore[index]


def test_ut02_55_extra_entities_scanned(tmp_path: Path) -> None:
    """UT02-55 config-defined entities are scanned too, duplicates collapse."""
    layout = DataLayout.from_root(tmp_path / "data")
    _commit(layout.raw, "files", "budget", {"amount": ["10"]})
    inv = scan_lake(layout, extra_entities=[("files", "budget"), ("files", "budget")])
    assert inv.get("files", "budget").present is True
    assert "amount" in inv.get("files", "budget").columns
    assert len(inv.entities) == len(EXPECTED_ENTITIES) + 1


def test_ut02_55_synth_root(tmp_path: Path) -> None:
    """UT02-55 a data/synth root marks every entity and the inventory from_synth."""
    layout = DataLayout.from_root(tmp_path / "data" / "synth")
    _commit(layout.raw, "jira", "issue", {"summary_len": ["3"]})
    inv = scan_lake(layout)
    assert inv.from_synth is True
    assert inv.get("jira", "issue").from_synth is True


def test_ut02_55_synth_file_path(tmp_path: Path) -> None:
    """UT02-55 a file whose resolved path has data/synth sets from_synth without the marker."""
    layout = DataLayout.from_root(tmp_path / "plain")
    target = tmp_path / "data" / "synth" / "issue"
    _commit(target.parent, "jira", "issue", {"a": ["1"]})
    link = layout.raw / "jira" / "issue"
    link.parent.mkdir(parents=True)
    try:
        link.symlink_to(target.parent / "jira" / "issue", target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    inv = scan_lake(layout)
    assert layout.synth_marker is False
    assert inv.get("jira", "issue").from_synth is True
    assert inv.from_synth is True


def test_ut02_55_synth_file_path_without_marker(tmp_path: Path) -> None:
    """UT02-55 files under data/synth set from_synth even if the layout marker is off."""
    raw = tmp_path / "data" / "synth" / "raw"
    layout = DataLayout(
        root=tmp_path,
        raw=raw,
        warehouse=tmp_path / "warehouse",
        ops_db=tmp_path / "ops.sqlite",
        vectors=tmp_path / "vectors",
        synth_marker=False,
    )
    _commit(raw, "monitoring", "event", {"severity": ["critical"]})
    inv = scan_lake(layout)
    assert inv.get("monitoring", "event").from_synth is True
    assert inv.get("jira", "issue").from_synth is False
    assert inv.from_synth is True


def test_ut02_55_unreadable_file(tmp_path: Path) -> None:
    """UT02-55 a committed-looking file that is not Parquet raises SchemaViolation."""
    layout = DataLayout.from_root(tmp_path / "data")
    bad = layout.raw / "jira" / "issue" / "dt=2026-09-01" / "part-bad.parquet"
    bad.parent.mkdir(parents=True)
    bad.write_bytes(b"garbage")
    with pytest.raises(SchemaViolation, match=r"unreadable lake file jira/issue/"):
        scan_lake(layout)


def test_ut02_55_vanished_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-55 a file removed between listing and stat is an unreadable file, not OSError."""
    layout = DataLayout.from_root(tmp_path / "data")
    _commit(layout.raw, "jira", "issue", {"a": ["1"]})
    real = lakeinfo.pq.read_schema

    def read_then_remove(path: Path) -> pa.Schema:
        schema = real(path)
        path.unlink()
        return schema

    monkeypatch.setattr(lakeinfo.pq, "read_schema", read_then_remove)
    with pytest.raises(SchemaViolation, match=r"unreadable lake file jira/issue/"):
        scan_lake(layout)


def test_ut02_55_too_many_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT02-55 more files than the limit raises SchemaViolation naming the entity."""
    monkeypatch.setattr(lakeinfo, "MAX_FILES_PER_ENTITY", 1)
    layout = DataLayout.from_root(tmp_path / "data")
    _commit(layout.raw, "jira", "issue", {"a": ["1"]})
    _commit(layout.raw, "jira", "issue", {"a": ["2"]})
    with pytest.raises(SchemaViolation, match="too many lake files for jira/issue"):
        scan_lake(layout)


def test_ut02_55_invariant_present_iff_files() -> None:
    """UT02-55 EntityInventory rejects present without files and files without present."""
    with pytest.raises(ValueError, match="present"):
        EntityInventory("jira", "issue", "g", True, 0, 0, frozenset(), False)
    with pytest.raises(ValueError, match="present"):
        EntityInventory("jira", "issue", "g", False, 1, 0, frozenset(), False)
