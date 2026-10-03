"""LoRA JSONL export of active qa_pairs (impl 07 U07-92; design 07 §5.11 "LoRA export").

Golden questions are passed in (memory never imports `herness.eval`, DD29). Path, golden and
secrets-sink checks live in `_lora_checks`; a line is built whole (never truncated), dropped
over LINE_MAX_BYTES and scrubbed once more. Files go to a temp dir that is fsynced and renamed,
or removed. Logs carry counts and ids only.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Final, cast

import duckdb
import numpy as np

from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.ids import canonical_json, new_ulid
from herness.core.logging import get_logger
from herness.core.redact import Redactor
from herness.harness.memory import _lora_checks as chk
from herness.harness.memory.policy import InjectionScanner, keyed_hash
from herness.harness.memory.procedural import wilson_lower_bound
from herness.harness.memory.settings import LoraConfig
from herness.harness.memory.types import MEMORY_ID_RE, ExportReport
from herness.metrics.catalog import load_catalog
from herness.store import ops, warehouse

__all__ = ["LINE_MAX_BYTES", "LORA_SYSTEM_PROMPT", "SYSTEM_PROMPT_VERSION", "LoraDeps",
           "export_lora"]  # fmt: skip

LORA_SYSTEM_PROMPT: Final = (
    "You translate a question about IT operations into exactly one read-only DuckDB SELECT"
    " query over the Herness warehouse schemas core, enrich, metrics and score."
    " Use only the tables, columns and metrics described below. Reply with the SQL only."
)
SYSTEM_PROMPT_VERSION: Final = "t2s-v1"
LINE_MAX_BYTES: Final = 32_768  # UTF-8 bytes per JSONL line, newline excluded
_COLUMNS_SQL: Final = (
    "SELECT table_schema, table_name, column_name, data_type FROM information_schema.columns"
    " WHERE table_schema IN ('core', 'enrich', 'metrics', 'score') ORDER BY 1, 2, 3, 4"
)
_ID_PAGE, _ROW_PAGE = 10_000, 500
_FILES: Final = ("train", "val")
_EXCLUDED: Final = ("excluded_golden", "excluded_low_pass_lb", "excluded_unsafe",
                    "excluded_oversize")  # fmt: skip
_log = get_logger("memory")


def _catalog_names() -> Sequence[str]:
    return load_catalog().names()


@dataclass(frozen=True, slots=True)
class LoraDeps:
    """Collaborators of `export_lora`; `export_root` is `<data root>/models/lora_data`."""

    conn_factory: Callable[[], sqlite3.Connection]
    embedder: chk.Embeds
    redactor: Redactor
    scanner: InjectionScanner
    config: LoraConfig
    export_root: Path
    config_hash: str
    open_warehouse: Callable[[], duckdb.DuckDBPyConnection] = warehouse.open_readonly
    metric_names: Callable[[], Sequence[str]] = _catalog_names


type _Out = tuple[str, bytes] | None


@dataclass(frozen=True, slots=True)
class _Tpl:
    fingerprint: str
    pass_lb: float
    build_id: str


@dataclass(slots=True)
class _Run:
    """One export's inputs and running tallies (memory O(templates))."""

    deps: LoraDeps
    min_pass_lb: float
    goldens: np.ndarray | None
    system: str
    digest: str
    counts: dict[str, int] = field(default_factory=lambda: dict.fromkeys(_EXCLUDED, 0))
    fps: dict[str, set[str]] = field(default_factory=lambda: {f: set() for f in _FILES})
    used: set[str] = field(default_factory=set)
    cache: dict[str, _Tpl | None] = field(default_factory=dict)

    def drop(self, reason: str) -> _Out:
        self.counts[reason] += 1
        return None


def _template(template_id: object, run: _Run, conn: sqlite3.Connection) -> _Tpl | None:
    """The pair's active template with its Wilson pass_lb, else None (cached by id)."""
    if not isinstance(template_id, str) or MEMORY_ID_RE.fullmatch(template_id) is None:
        return None
    if template_id not in run.cache:
        rows = ops.get_memory_items([template_id], conn=conn)
        tpl = None
        if rows and rows[0]["kind"] == "sql_template" and rows[0]["status"] == "active":
            d = rows[0]["data"]
            n = [v if isinstance(v := d.get(k), int) else 0 for k in ("passes", "fails")]
            tpl = _Tpl(str(d.get("fingerprint")), wilson_lower_bound(*n),
                       str(d.get("build_id_last_ok") or ""))  # fmt: skip
        run.cache[template_id] = tpl
    return run.cache[template_id]


def _split(fingerprint: str, val_fraction: float) -> str:
    bucket = int(keyed_hash(fingerprint)[:8], 16) % 10000  # SHA-256 prefix (UT05-124 list)
    return "val" if bucket < val_fraction * 10000 else "train"


def _line(row: ops.MemoryItemRow, run: _Run, conn: sqlite3.Connection) -> _Out:
    """Steps 2-4 and 6 for one pair: (split, line bytes), or None after counting the drop."""
    data = cast("dict[str, object]", row["data"])
    tpl = _template(data.get("template_id"), run, conn)
    if tpl is None or tpl.pass_lb < run.min_pass_lb:
        return run.drop("excluded_low_pass_lb")
    pair = chk.safe_pair(data.get("question"), data.get("sql"), run.deps)
    if pair is None:
        return run.drop("excluded_unsafe")
    if chk.near(pair[0], run.goldens, run.deps):
        return run.drop("excluded_golden")
    messages = [{"role": "system", "content": run.system}, {"role": "user", "content": pair[0]},
                {"role": "assistant", "content": pair[1]}]  # fmt: skip
    meta = {"template_id": data["template_id"], "fingerprint": tpl.fingerprint,
            "pass_lb": round(tpl.pass_lb, 2), "build_id": tpl.build_id,
            "schema_digest": run.digest}  # fmt: skip
    line = canonical_json({"id": row["memory_id"], "messages": messages, "meta": meta})
    if len(raw := line.encode("utf-8")) > LINE_MAX_BYTES:
        return run.drop("excluded_oversize")
    if chk.scrubbed(line) != line:
        return run.drop("excluded_unsafe")
    split = _split(tpl.fingerprint, run.deps.config.val_fraction)
    run.fps[split].add(tpl.fingerprint)
    run.used.add(str(data["template_id"]))
    return split, raw


def _active_pairs(conn: sqlite3.Connection, stamp: str) -> Iterator[ops.MemoryItemRow]:
    """Active qa_pairs in memory_id order, paged (streaming; step 2)."""
    after = ""
    while True:
        rows = ops.maintenance_rows(selector="all_ids_status", now=stamp, limit=_ID_PAGE,
                                    after=after, conn=conn)  # fmt: skip
        page = cast("list[tuple[str, str]]", rows)
        ids = [memory_id for memory_id, status in page if status == "active"]
        for start in range(0, len(ids), _ROW_PAGE):
            rows = ops.get_memory_items(ids[start : start + _ROW_PAGE], conn=conn)
            yield from (r for r in rows if r["kind"] == "qa_pair")
        if len(page) < _ID_PAGE:
            return
        after = page[-1][0]


def _schema_digest(deps: LoraDeps) -> str:
    """Step 5: 16 hex over the sorted digest-schema columns of the CURRENT build."""
    con = deps.open_warehouse()
    try:
        rows = [list(r) for r in con.execute(_COLUMNS_SQL).fetchall()]
    finally:
        con.close()
    return keyed_hash(canonical_json(sorted(rows)))[:16]


def _file_sha(path: Path) -> str:
    """SHA-256 of a written export file (not a result-row hash, UT05-124)."""
    with path.open("rb") as fh:
        return hashlib.file_digest(fh, "sha256").hexdigest()


def _write(tmp: Path, run: _Run, stamp: str) -> tuple[dict[str, int], dict[str, str]]:
    """Step 6: stream both JSONL files (fsynced); return their counts and SHA-256s."""
    counts = dict.fromkeys(_FILES, 0)
    with (tmp / "train.jsonl").open("wb") as train, (tmp / "val.jsonl").open("wb") as val:
        handles = {"train": train, "val": val}
        conn = run.deps.conn_factory()
        for row in _active_pairs(conn, stamp):
            if (out := _line(row, run, conn)) is not None:
                handles[out[0]].write(out[1] + b"\n")
                counts[out[0]] += 1
        for fh in (train, val):
            fh.flush()
            os.fsync(fh.fileno())
    assert run.fps["train"].isdisjoint(run.fps["val"])  # noqa: S101 - U07-92 invariant
    return counts, {f"{f}.jsonl": _file_sha(tmp / f"{f}.jsonl") for f in _FILES}


def _manifest(tmp: Path, run: _Run, body: dict[str, object]) -> str:
    """Step 7: canonical manifest.json (fsynced); returns its SHA-256."""
    cfg = run.deps.config
    body |= run.counts | {
        "filters": {"min_pass_lb": run.min_pass_lb, "val_fraction": cfg.val_fraction,
                    "golden_exclusion_cosine": cfg.golden_exclusion_cosine},
        "system_prompt_version": SYSTEM_PROMPT_VERSION, "schema_digest": run.digest,
        "config_hash": run.deps.config_hash, "templates": len(run.used),
        "golden_exclusion": "none" if run.goldens is None else "cosine",
        "line_max_bytes": LINE_MAX_BYTES,
    }  # fmt: skip
    raw = canonical_json(body).encode("utf-8")
    with (tmp / "manifest.json").open("wb") as fh:
        fh.write(raw)
        fh.flush()
        os.fsync(fh.fileno())
    return _file_sha(tmp / "manifest.json")


def export_lora(
    out_dir: Path,
    min_pass_lb: float = 0.8,
    *,
    golden_questions: Sequence[str],
    deps: LoraDeps,
    now: datetime | None = None,
) -> ExportReport:
    """Write `out_dir/<export_id>/{train,val}.jsonl` and `manifest.json` atomically (U07-92)."""
    target = chk.contained(out_dir, deps.export_root)
    stamp = clock.format_utc(now or clock.now())
    names = ", ".join(deps.metric_names())
    digest = _schema_digest(deps)
    system = f"{LORA_SYSTEM_PROMPT}\nSchema digest: {digest}\nMetrics: {names}"
    run = _Run(deps, min_pass_lb, chk.goldens(golden_questions, deps), system, digest)
    export_id = new_ulid()
    tmp, final = target / f".tmp-{export_id}", target / export_id
    try:
        target.mkdir(parents=True, exist_ok=True)
        tmp.mkdir()
        lines, sha = _write(tmp, run, stamp)
        counts = {"train_count": lines["train"], "val_count": lines["val"]} | run.counts
        body: dict[str, object] = {"export_id": export_id, "created_at": stamp, "sha256": sha}
        body |= counts
        manifest_sha = _manifest(tmp, run, body)
        os.replace(tmp, final)
    except BaseException as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        if isinstance(exc, OSError):
            msg = f"lora export write failed: {target}"
            raise ConfigError(msg) from exc
        raise
    _log.info("memory.lora.exported", export_id=export_id, templates=len(run.used), **counts)
    return ExportReport(
        export_id=export_id, out_dir=final, train_count=counts["train_count"],
        val_count=counts["val_count"], excluded_golden=run.counts["excluded_golden"],
        excluded_low_pass_lb=run.counts["excluded_low_pass_lb"], templates=len(run.used),
        config_hash=deps.config_hash, manifest_sha256=manifest_sha,
    )  # fmt: skip
