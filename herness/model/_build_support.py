"""Private helpers of ``herness.model.build`` (T02-18 size split; impl 02 §2 module-map note).

Build resolution (U02-98 step 3), the orphan deletion that stands in for ``cleanup_builds``
until T02-21, the DuckDB form of ``build.memory_limit`` (T02-18 spec note: DuckDB rejects
``%``) and the §8.2 metric samples. Nothing here opens a writable warehouse connection.
"""

from __future__ import annotations

import dataclasses
import datetime
import re
from collections.abc import Mapping
from typing import Final

import psutil
from pydantic import JsonValue

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.types import MetricSample
from herness.model.settings import BuildSettings
from herness.store import ops, warehouse
from herness.store.errors import NotFoundError
from herness.store.layout import DataLayout

STAGE_ORDER: Final = ("build", "enrich", "score", "dq", "promote")

_MIB: Final = 1024 * 1024
_MIN_MEMORY: Final = 256 * _MIB
_PERCENT_RE: Final = re.compile(r"^([1-9][0-9]?|100)%$")
_SIZE_RE: Final = re.compile(r"^([0-9]+(?:\.[0-9]+)?)\s?(GB|MB|GiB|MiB)$")
_UNIT_BYTES: Final = {"MB": 1000**2, "GB": 1000**3, "MiB": _MIB, "GiB": 1024**3}
_COMPONENT: Final = "model"

_log = get_logger("model.build")


@dataclasses.dataclass(frozen=True, slots=True)
class DbSettings:
    """The ``build`` settings handed to ``open_for_build``, memory limit in DuckDB units."""

    threads: int | None
    memory_limit: str


def _total_memory() -> int:
    return int(psutil.virtual_memory().total)


def resolve_memory_limit(value: str) -> str:
    """``build.memory_limit`` in a unit DuckDB accepts (T02-18 spec note).

    A percentage (1-100) becomes MiB of total physical RAM, at least 256 MiB; a size passes
    through unchanged when it is at least 256 MiB. Anything else is ConfigError.
    """
    percent = _PERCENT_RE.fullmatch(value)
    if percent is not None:
        size = max(_total_memory() * int(percent.group(1)) // 100, _MIN_MEMORY)
        return f"{size // _MIB}MiB"
    sized = _SIZE_RE.fullmatch(value)
    if sized is None or float(sized.group(1)) * _UNIT_BYTES[sized.group(2)] < _MIN_MEMORY:
        msg = "build.memory_limit must be 1-100% or a size of at least 256 MiB"
        raise ConfigError(msg)
    return value


def db_settings(build_cfg: BuildSettings) -> DbSettings:
    """A converted view of ``build_cfg`` for ``open_for_build`` (which passes it verbatim)."""
    return DbSettings(build_cfg.threads, resolve_memory_limit(build_cfg.memory_limit))


def _done_stages(state: Mapping[str, JsonValue], build_id: object) -> list[str]:
    done = state.get("stages_done")
    if state.get("build_id") != build_id or not isinstance(done, list):
        return []
    return [stage for stage in STAGE_ORDER if stage in done]


def _status(build_id: str, layout: DataLayout) -> str | None:
    found = [b.status for b in warehouse.list_builds(layout=layout) if b.build_id == build_id]
    return found[0] if found else None


def resolve_build(
    build_id: str | None,
    state: Mapping[str, JsonValue],
    layout: DataLayout,
    now: datetime.datetime,
) -> tuple[str, list[str]]:
    """The build to work on and its stages already done (U02-98 step 3).

    A payload ``build_id`` must exist (NotFoundError) with status ``building`` (ConfigError);
    else a state naming a ``building`` build with ``build`` done resumes it; else a new ID.
    """
    if build_id is not None:
        status = _status(build_id, layout)
        if status is None:
            msg = f"build {build_id} does not exist"
            raise NotFoundError(msg, kind="build", key=build_id)
        if status != "building":
            msg = f"build {build_id} is {status}"
            raise ConfigError(msg)
        return build_id, _done_stages(state, build_id)
    state_id = state.get("build_id")
    done = _done_stages(state, state_id)
    if isinstance(state_id, str) and "build" in done and _status(state_id, layout) == "building":
        return state_id, done
    return warehouse.new_build_id(now), []


def delete_orphans(layout: DataLayout, *, protect: str) -> None:
    """Delete ``building`` files with no ``finished_at`` (crashed, killed or yielded builds).

    T02-21: replace with ``cleanup_builds(mode="pre", protect=frozenset({build_id}), …)``,
    which adds the unreadable and failed-build rules and the builds pinned by runs.
    """
    for info in warehouse.list_builds(layout=layout):
        orphan = info.status == "building" and info.finished_at is None
        if not orphan or info.is_current or info.build_id == protect:
            continue
        if warehouse.delete_build_files(info.build_id, layout=layout) == "deleted":
            _log.info("model.build.orphan_deleted", build_id=info.build_id, status=info.status)


def _sample(name: str, kind: str, value: float, **labels: str) -> MetricSample:
    fields = {"ts": clock.now(), "name": name, "kind": kind, "value": float(value)}
    return MetricSample.model_validate(fields | {"labels": labels, "component": _COMPONENT})


def write_metrics(
    *,
    build_id: str,
    layout: DataLayout,
    status: str,
    stage_ms: Mapping[str, int],
    sql_ms: Mapping[str, int],
    row_counts: Mapping[str, int],
) -> None:
    """§8.2 samples through T08-05; a failure is logged, never raised (F02-02 step 10)."""
    try:
        samples = [_sample("herness_model_build_total", "counter", 1, status=status)]
        samples += [
            _sample("herness_model_build_stage_seconds", "histogram", ms / 1000, stage=stage)
            for stage, ms in stage_ms.items()
        ]
        samples += [
            _sample("herness_model_build_sql_file_seconds", "histogram", ms / 1000, file=file)
            for file, ms in sql_ms.items()
        ]
        samples += [
            _sample("herness_model_build_rows_total", "gauge", rows, table=table)
            for table, rows in row_counts.items()
            if table.startswith("core.")
        ]
        path = warehouse.build_path(build_id, layout=layout)
        if path.is_file():
            samples.append(_sample("herness_model_warehouse_bytes", "gauge", path.stat().st_size))
        ops.record_metric_samples(samples)
    except Exception as exc:  # noqa: BLE001 - metrics never fail the build (F02-02 step 10)
        _log.warning("model.build.metrics_write_failed", error_class=type(exc).__name__)
