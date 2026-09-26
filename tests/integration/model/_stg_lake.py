"""Lake-writing helpers for the staging integration tests (impl 02 T02-13).

Rows reach the lake through the real `LakeWriter` (U02-13), so files carry the metadata
columns of `META_COLUMNS` and land in `dt=` partitions exactly as connectors write them.
Entity values are text, as impl 01's `flatten_record` writes them.
"""

from __future__ import annotations

import dataclasses
import datetime
import json
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import pyarrow as pa
from tests.support.build_harness import BuildHarness, RefData

from herness.model.lakeinfo import scan_lake
from herness.model.settings import CustomFieldsConfig, MappingsConfig
from herness.store.lake import LakeWriter

UTC = datetime.UTC
T0 = datetime.datetime(2024, 3, 1, 12, 0, tzinfo=UTC)
_TS = pa.timestamp("us", tz="UTC")


def at(hours: int) -> datetime.datetime:
    """`T0` plus `hours` hours."""
    return T0 + datetime.timedelta(hours=hours)


@dataclasses.dataclass(frozen=True)
class Row:
    """One lake row: source key, update time, fields; `deleted` makes a tombstone."""

    key: str
    updated: datetime.datetime
    fields: Mapping[str, str | None] = dataclasses.field(default_factory=dict)
    deleted: bool = False
    fetched: datetime.datetime | None = None


def _batch(source: str, entity: str, rows: Sequence[Row]) -> pa.RecordBatch:
    names = list(dict.fromkeys(name for row in rows for name in row.fields))
    columns: dict[str, pa.Array] = {
        "_record_id": pa.array([f"{source}:{entity}:{r.key}" for r in rows], pa.string()),
        "_source": pa.array([source] * len(rows), pa.string()),
        "_entity": pa.array([entity] * len(rows), pa.string()),
        "_source_key": pa.array([r.key for r in rows], pa.string()),
        "_source_updated_at": pa.array([r.updated for r in rows], _TS),
        "_fetched_at": pa.array([r.fetched or r.updated for r in rows], _TS),
        "_deleted": pa.array([r.deleted for r in rows], pa.bool_()),
        "_payload": pa.array(
            [None if r.deleted else json.dumps(dict(r.fields)) for r in rows], pa.string()
        ),
    }
    for name in names:
        columns[name] = pa.array([r.fields.get(name) for r in rows], pa.string())
    return pa.RecordBatch.from_pydict(columns)


def commit(raw: Path, source: str, entity: str, rows: Sequence[Row]) -> list[Path]:
    """Write `rows` as one committed lake file set; return the committed paths."""
    with LakeWriter(source, entity, root=raw) as writer:
        writer.write(_batch(source, entity, rows))
        return list(writer.commit().files)


def open_writer(raw: Path, source: str, entity: str, rows: Sequence[Row]) -> LakeWriter:
    """A writer holding `rows` in flushed temp files, not committed (caller aborts it)."""
    writer = LakeWriter(source, entity, root=raw)
    writer.write(_batch(source, entity, rows))
    return writer


def build(
    harness: BuildHarness,
    *,
    enums: Mapping[str, Mapping[str, str]] | None = None,
    custom_fields: Mapping[str, Mapping[str, str]] | None = None,
    deleted_ids: Iterable[str] = (),
    lo: int = 0,
    hi: int = 199,
) -> list[str]:
    """Scan the harness lake and run files lo..hi with the given reference data."""
    inventory = scan_lake(harness.layout)
    fields = CustomFieldsConfig.model_validate(dict(custom_fields or {}))
    refdata = RefData(
        mappings=MappingsConfig.model_validate({"enums": dict(enums or {})}),
        deleted_ids=list(deleted_ids),
    )
    context = harness.context(inventory, custom_fields=fields)
    return harness.run(lo, hi, refdata=refdata, context=context)


def columns(harness: BuildHarness, table: str) -> list[tuple[str, str]]:
    """(name, type) of every column of `stg.<table>`, in order."""
    rows = harness.query(
        "SELECT column_name, data_type FROM information_schema.columns"
        " WHERE table_schema = 'stg' AND table_name = ? ORDER BY ordinal_position",
        [table],
    )
    return [(str(name), str(kind)) for name, kind in rows]


def cast_stats(harness: BuildHarness, table: str) -> dict[str, tuple[int, int]]:
    """`stg.cast_stats` of one table: column -> (non_null, failed)."""
    rows = harness.query(
        "SELECT column_name, non_null, failed FROM stg.cast_stats WHERE table_name = ?",
        [f"stg.{table}"],
    )
    out: dict[str, tuple[int, int]] = {}
    for name, non_null, failed in rows:
        assert str(name) not in out, f"duplicate cast_stats row for {name}"
        out[str(name)] = (int(str(non_null)), int(str(failed)))
    return out
