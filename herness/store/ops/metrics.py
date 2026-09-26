"""Metrics ops-store area (impl 08 U08-100, R-12): the single `metric_sample` writer and its
90-day retention purge.

The table is impl 02 migration 006 (§4.3.6). Counters, gauges and histograms all arrive as
`MetricSample` values (U08-101); the kind travels in `MetricSample.kind`. Every write goes
through `herness.store.ops.core.run_write` unless the caller hands in its own connection.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from datetime import datetime
from typing import Final

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.ids import canonical_json
from herness.core.types import MetricSample

from .core import run_write

MAX_SAMPLES_PER_CALL: Final = 100_000  # TH08-10 row cap per call
CHUNK_ROWS: Final = 500  # rows per writer transaction
LABELS_MAX_BYTES: Final = 1024  # the table's CHECK (length(labels) <= 1024)

_INSERT: Final = (
    "INSERT INTO metric_sample (ts, name, kind, value, labels, component) VALUES (?, ?, ?, ?, ?, ?)"
)

type _Params = tuple[str, str, str, float, str, str]


def _params(sample: MetricSample) -> _Params:
    labels = canonical_json(sample.labels)
    if len(labels.encode("utf-8")) > LABELS_MAX_BYTES:
        msg = f"labels of metric {sample.name} exceed {LABELS_MAX_BYTES} bytes"
        raise ConfigError(msg)
    ts = clock.format_utc(sample.ts)
    return (ts, sample.name, sample.kind, float(sample.value), labels, sample.component)


def record_metric_samples(
    samples: Sequence[MetricSample], *, conn: sqlite3.Connection | None = None
) -> int:
    """Insert one `metric_sample` row per sample and return the row count (U08-100).

    With ``conn`` the rows join the caller's open `run_write` transaction; without it they are
    written in chunks of 500 rows, each chunk in its own `run_write(op="metric_samples")`.
    Raises ConfigError (more than 100 000 samples, labels over 1 024 bytes), StoreBusy or
    SchemaViolation (from `run_write`).
    """
    if len(samples) > MAX_SAMPLES_PER_CALL:
        msg = "too many metric samples"
        raise ConfigError(msg)
    if not samples:
        return 0
    rows = [_params(sample) for sample in samples]
    if conn is not None:
        conn.executemany(_INSERT, rows)
        return len(rows)
    for start in range(0, len(rows), CHUNK_ROWS):
        chunk = rows[start : start + CHUNK_ROWS]

        def insert(c: sqlite3.Connection, chunk: list[_Params] = chunk) -> None:
            c.executemany(_INSERT, chunk)

        run_write(insert, op="metric_samples")
    return len(rows)


def purge_metric_samples(before: datetime) -> int:
    """Delete `metric_sample` rows older than ``before`` and return the count (U08-100).

    Called with ``now - 90 days`` by the `herness.admin` retention purge (R-07, O08-09).
    """
    cutoff = clock.format_utc(before)

    def delete(conn: sqlite3.Connection) -> int:
        return conn.execute("DELETE FROM metric_sample WHERE ts < ?", (cutoff,)).rowcount

    return run_write(delete, op="metric_purge")
