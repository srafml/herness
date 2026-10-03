"""Private sibling of `tools.synth.shards` (U11-19): lake sink, truth parts, incident index.

Split off for the 350-line budget of `shards.py`; imported only by `tools.synth._shard_run`
and `tools.synth.truth_writer`. Truth parts are per-shard Parquet files under
`<root>/truth/.parts/` (`labels-`, `texts-`, `pii-`, `members-`, `pairs-<index>.parquet`);
the incident time index of phase A is one set of NumPy `.npy` arrays per month under
`.parts/idx/` (no pickles). Everything here writes tmp-then-`os.replace`.
"""

import functools
import os
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import numpy as np
import numpy.typing as npt
import pyarrow as pa
import pyarrow.parquet as pq

from herness.store.lake import LakeWriter
from tools.synth.flatten import MAX_ROWS, to_lake_batch
from tools.synth.servicenow_common import Record, parse_ts
from tools.synth.shards import TARGET_BYTES, IncidentTimeIndex

Row = Mapping[str, Any]

PART_SCHEMAS: Final = {
    "labels": pa.schema([("record_id", pa.string()), ("question", pa.string()),
                         ("answer", pa.string())]),
    "texts": pa.schema([("record_id", pa.string()), ("short_description", pa.string()),
                        ("description", pa.string())]),
    "pii": pa.schema([("record_id", pa.string()), ("field", pa.string()), ("start", pa.int64()),
                      ("end", pa.int64()), ("type", pa.string())]),
    "members": pa.schema([("record_id", pa.string()), ("plant", pa.string())]),
    "pairs": pa.schema([("incident_record_id", pa.string()), ("change_record_id", pa.string())]),
}  # fmt: skip
_IDX_FIELDS: Final = ("opened", "service", "sys_id", "number", "priority")
IDX_DIR: Final = "idx"


class LakeSink:
    """One `LakeWriter` per `(source, entity)` under `raw_root`; `abort` discards them all."""

    def __init__(self, raw_root: Path) -> None:
        self._raw = raw_root
        self._writers: dict[tuple[str, str], LakeWriter] = {}
        self.rows: dict[str, int] = {}

    def write(
        self,
        source: str,
        entity: str,
        rows: Sequence[tuple[Row, datetime]],
        *,
        bare: Callable[[], bool] | None = None,
    ) -> None:
        """Flatten `rows` in batches of at most `MAX_ROWS`; `bare` is the per-file drift coin."""
        key = (source, entity)
        if key not in self._writers:
            writer = LakeWriter(source, entity, target_bytes=TARGET_BYTES, root=self._raw)
            self._writers[key] = writer
        for start in range(0, len(rows), MAX_ROWS):
            chunk = rows[start : start + MAX_ROWS]
            batch = to_lake_batch(source, entity, chunk, bare_priority=bool(bare and bare()))
            self._writers[key].write(batch)
        name = f"{source}/{entity}"
        self.rows[name] = self.rows.get(name, 0) + len(rows)

    def commit(self) -> tuple[Path, ...]:
        files: list[Path] = []
        for writer in self._writers.values():
            files.extend(writer.commit().files)
        return tuple(files)

    def abort(self) -> None:
        for writer in self._writers.values():
            writer.abort()


def _replace_write(path: Path, write: Callable[[Path], None]) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    try:
        write(tmp)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def write_table(path: Path, table: pa.Table, *, row_group_size: int | None = None) -> None:
    """Parquet (zstd) tmp-then-`os.replace`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    _replace_write(
        path,
        lambda tmp: pq.write_table(table, tmp, compression="zstd", row_group_size=row_group_size),
    )


def write_part(parts_dir: Path, kind: str, index: int, rows: Sequence[Row]) -> Path | None:
    """`<kind>-<index>.parquet` of `rows` under `parts_dir`; None (no file) when empty."""
    if not rows:
        return None
    schema = PART_SCHEMAS[kind]
    table = pa.Table.from_pylist([{n: r.get(n) for n in schema.names} for r in rows], schema)
    path = parts_dir / f"{kind}-{index:05d}.parquet"
    write_table(path, table)
    return path


def read_parts(parts_dir: Path, kind: str) -> pa.Table:
    """All `<kind>-*.parquet` parts in shard order (an empty table when there are none)."""
    paths = sorted(parts_dir.glob(f"{kind}-*.parquet")) if parts_dir.is_dir() else []
    tables = [pq.read_table(p, schema=PART_SCHEMAS[kind]) for p in paths]
    return pa.concat_tables(tables) if tables else PART_SCHEMAS[kind].empty_table()


def write_index(parts_dir: Path, month: date, records: Sequence[Record]) -> None:
    """Phase A: the month's incidents (open time, service, sys_id, number, priority)."""
    rows = []
    for rec in records:
        opened, service = parse_ts(rec["opened_at"]), rec["business_service"]["value"]
        if opened is not None and service:
            values = (rec["sys_id"]["value"], rec["number"]["value"], rec["priority"]["value"])
            rows.append((int(opened.timestamp()), service, *values))
    columns = list(zip(*rows, strict=True)) if rows else [()] * len(_IDX_FIELDS)
    directory = parts_dir / IDX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    for name, column in zip(_IDX_FIELDS, columns, strict=True):
        array = np.array(column, dtype=np.int64 if name == "opened" else np.str_)
        path = directory / f"{month:%Y-%m}.{name}.npy"
        _replace_write(path, functools.partial(_save, array=array))


def _save(path: Path, array: npt.NDArray[Any]) -> None:
    with path.open("wb") as handle:
        np.save(handle, array, allow_pickle=False)


def _months(lo: datetime, hi: datetime) -> list[date]:
    out, month = [], lo.date().replace(day=1)
    while month <= hi.date():
        out.append(month)
        month = (month.replace(day=28) + timedelta(days=4)).replace(day=1)
    return out


def _load(directory: Path, month: date) -> list[npt.NDArray[Any]] | None:
    paths = [directory / f"{month:%Y-%m}.{name}.npy" for name in _IDX_FIELDS]
    if not all(p.is_file() for p in paths):
        return None
    return [np.load(p, allow_pickle=False) for p in paths]


def load_index(
    parts_dir: Path, lo: datetime, hi: datetime
) -> tuple[IncidentTimeIndex, frozenset[tuple[str, date]]]:
    """Index of the incidents opened in `[lo, hi)` per service, sorted by open time, and the
    `(service sys_id, day)` pairs with a P1 incident (the `metric_daily` dips)."""
    loaded = [x for m in _months(lo, hi) if (x := _load(parts_dir / IDX_DIR, m)) is not None]
    if not loaded:
        return IncidentTimeIndex({}, {}, {}), frozenset()
    opened, service, sys_id, number, priority = (
        np.concatenate([part[i] for part in loaded]) for i in range(len(_IDX_FIELDS))
    )
    keep = (opened >= int(lo.timestamp())) & (opened < int(hi.timestamp()))
    opened, service, sys_id = opened[keep], service[keep], sys_id[keep]
    number, priority = number[keep], priority[keep]
    order = np.lexsort((opened, service))
    times: dict[str, npt.NDArray[np.int64]] = {}
    ids: dict[str, tuple[str, ...]] = {}
    numbers: dict[str, tuple[str, ...]] = {}
    for svc in dict.fromkeys(service[order].tolist()):
        rows = order[service[order] == svc]
        times[svc] = opened[rows].astype(np.int64)
        ids[svc], numbers[svc] = tuple(sys_id[rows].tolist()), tuple(number[rows].tolist())
    p1 = frozenset(
        (str(s), datetime.fromtimestamp(int(t), UTC).date())
        for s, t, p in zip(service, opened, priority, strict=True)
        if p == "1"
    )
    return IncidentTimeIndex(times, ids, numbers), p1


__all__ = [
    "IDX_DIR",
    "PART_SCHEMAS",
    "LakeSink",
    "load_index",
    "read_parts",
    "write_index",
    "write_part",
    "write_table",
]
