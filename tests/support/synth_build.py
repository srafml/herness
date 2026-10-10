"""Build a synthetic dataset through the real pipeline and snapshot it (impl 02 T02-22).

`generate` runs `tools/synth_data.py` (seed 42, scale `small`, `--dirty default`) in a
subprocess; `build_synth` loads a config tree whose `mappings.yaml` is the generator's
`synth_mappings.yaml`, runs `build_pipeline` through the job handler registry with every
stage (`build` ... `promote`) and the same fake spec 03 / 04 hooks as the IT02-21 tests, and
returns the result with the `core.*` row counts, the `meta.dq_result` rows and the per-table
digests (`core_digests`).

The digests depend only on data columns: rows are hashed whole (text columns included, so no
ticket text is stored), sorted, and combined; `LOAD_COLUMNS` (load and build time columns) are
left out, and nothing path- or run-specific (build id, ULID, absolute dataset root) enters.
"""

from __future__ import annotations

import base64
import dataclasses
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Final

import duckdb
import pytest
import yaml
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext
from tests.support.config_tree import SHIPPED, write_full_config

from herness.core import config as herness_config
from herness.core.jobs.handlers import register_handler, resolve_handler, run_handler
from herness.core.jobs.ports import JobContext
from herness.core.types import JobOutcome
from herness.model import _build_stages as stages
from herness.model.build import run_build_pipeline
from herness.store import ops, warehouse
from herness.store.layout import DataLayout
from herness.store.ops.migrate import migrate
from tools.synth import GENERATOR_VERSION

REPO: Final = Path(__file__).resolve().parents[2]
FULL_STAGES: Final = ["build", "enrich", "score", "dq", "promote"]
# Columns that hold load or build time rather than data (none is a data column of `core.*`
# today; the digest query skips a column of this name should one be added).
LOAD_COLUMNS: Final = frozenset({"_loaded_at", "_built_at", "_build_id", "_fetched_at"})
GEN_TIMEOUT_S: Final = 600


class _Report(BaseModel):
    name: str


def _enrichment(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="enrich")


def _scoring(*_args: object, **_kwargs: object) -> _Report:
    return _Report(name="scoring")


@dataclasses.dataclass(frozen=True)
class SynthBuild:
    """One pipeline run on a synthetic root: outcome, `core.*` counts, DQ rows, digests."""

    build_id: str
    promoted: bool
    result: dict[str, Any]
    row_counts: dict[str, int]
    dq: dict[str, dict[str, Any]]
    digests: dict[str, dict[str, Any]]
    truth: dict[str, Any]


def generate(root: Path, *, seed: int = 42, scale: str = "small") -> None:
    """Run the synthetic generator with `--dirty default` into `root` (`data/raw` below it)."""
    argv = [sys.executable, "tools/synth_data.py", "--seed", str(seed), "--scale", scale]
    argv += ["--dirty", "default", "--root", str(root)]
    env = {**os.environ, "PYTHONUTF8": "1"}
    done = subprocess.run(  # noqa: S603 - fixed argv, no shell
        argv, cwd=REPO, env=env, capture_output=True, text=True, timeout=GEN_TIMEOUT_S, check=False
    )
    assert done.returncode == 0, done.stdout[-2000:] + done.stderr[-2000:]


def _write_config(root: Path, cfg_root: Path) -> Path:
    cfg_dir = write_full_config(cfg_root)
    shipped = yaml.safe_load((SHIPPED / "mappings.yaml").read_text(encoding="utf-8"))
    synth = yaml.safe_load((root / "synth_mappings.yaml").read_text(encoding="utf-8"))["mappings"]
    # the generator's fragment overlays the shipped template (spec 11 U11-20): enum domains
    # merge value by value, the other sections are the fragment's.
    enums = {k: dict(v) for k, v in shipped.get("enums", {}).items()}
    for domain, values in synth.get("enums", {}).items():
        enums.setdefault(domain, {}).update(values)
    mappings = {**shipped, **{k: v for k, v in synth.items() if k != "enums"}, "enums": enums}
    (cfg_dir / "mappings.yaml").write_text(yaml.safe_dump(mappings), encoding="utf-8")
    (cfg_dir / "sources.yaml").write_text("version: 1\nbuild:\n  threads: 2\n", encoding="utf-8")
    return cfg_dir


def core_digests(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    """Per `core.*` base table: row count and a SHA-256 over the sorted whole-row hashes."""
    tables = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'core' ORDER BY table_name"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for (table,) in tables:
        names = [
            str(r[0])
            for r in con.execute(
                "SELECT column_name FROM duckdb_columns() WHERE schema_name = 'core'"
                " AND table_name = ? ORDER BY column_index",
                [table],
            ).fetchall()
        ]
        kept = [n for n in names if n not in LOAD_COLUMNS]
        cols = ", ".join(f'"{n}"' for n in kept)
        sql = (
            "SELECT count(*), coalesce(sha256(string_agg(h, '' ORDER BY h)), '') FROM"  # noqa: S608 - catalog names
            f' (SELECT sha256(CAST(row({cols}) AS VARCHAR)) AS h FROM core."{table}")'
        )
        count, digest = con.execute(sql).fetchone() or (0, "")
        # base32, not hex: a hex digest in a committed file trips detect-secrets
        text = "h:" + base64.b32encode(bytes.fromhex(digest)).decode("ascii").lower()
        out[f"core.{table}"] = {"rows": int(count), "columns": kept, "sha256": text}
    return out


def _dq_rows(con: duckdb.DuckDBPyConnection) -> dict[str, dict[str, Any]]:
    rows = con.execute(
        "SELECT check_name, severity, value, passed, CAST(details AS VARCHAR)"
        " FROM meta.dq_result ORDER BY check_name"
    ).fetchall()
    out: dict[str, dict[str, Any]] = {}
    for name, severity, value, passed, details in rows:
        info = {k: v for k, v in json.loads(details).items() if k != "producer"}
        out[str(name)] = {"severity": severity, "value": value, "passed": passed, "details": info}
    return out


def _handler(ctx: JobContext) -> JobOutcome:
    return run_build_pipeline(ctx)


def build_synth(root: Path, cfg_root: Path) -> SynthBuild:
    """Run the full pipeline on the generated `root` and snapshot its warehouse.

    Process-wide state (config, ops store binding, the spec 03 / 04 hooks, the data path
    variable) is set for the run and restored after it.
    """
    data_root = root / "data"
    cfg_dir = _write_config(root, cfg_root)
    truth = json.loads((root / "truth" / "truth.json").read_text(encoding="utf-8"))
    with pytest.MonkeyPatch.context() as patch:
        patch.setenv("HERNESS_PATHS__DATA", str(data_root))
        patch.setattr(stages, "_load_run_enrichment", lambda: _enrichment)
        patch.setattr(stages, "_load_run_scoring", lambda: _scoring)
        herness_config.reset_config()
        herness_config.init_config(
            "local", config_dir=cfg_dir, env={"HERNESS_PATHS__DATA": str(data_root)}
        )
        ops.reset_connections(path=data_root / "ops.sqlite")
        migrate()
        try:
            register_handler("build_pipeline", _handler)
            outcome = run_handler(
                FakeJobContext({"stages": list(FULL_STAGES)}), resolve_handler("build_pipeline")
            )
        finally:
            ops.reset_connections()
            herness_config.reset_config()
    assert isinstance(outcome, JobOutcome), outcome
    assert outcome.status == "done", outcome
    layout = DataLayout.from_root(data_root)
    current = warehouse.read_current(layout=layout)
    build_id = str(outcome.result["build_id"])
    with warehouse.open_readonly(build_id, layout=layout) as con:
        digests = core_digests(con)
        dq = _dq_rows(con)
    return SynthBuild(
        build_id=build_id,
        promoted=current == build_id and bool(outcome.result.get("promoted")),
        result=dict(outcome.result),
        row_counts={k: int(v) for k, v in dict(outcome.result["row_counts"]).items()},
        dq=dq,
        digests=digests,
        truth=truth,
    )


GOLDEN_DIR: Final = REPO / "tests" / "fixtures" / "golden"
GOLDEN_COUNTS: Final = GOLDEN_DIR / "synth_small_counts.json"
GOLDEN_CORE: Final = GOLDEN_DIR / "synth_small_core.json"
# `future_timestamp:*` compares with the build's wall-clock start and the generator shifts
# lifecycles by a fixed 730 days, so the number of future rows moves with the date.
CLOCK_DEPENDENT: Final = "future_timestamp:"


def golden_counts(built: SynthBuild) -> dict[str, Any]:
    """`core.*` row counts and the clock-independent `meta.dq_result` rows (as stored)."""
    dq = {k: v for k, v in built.dq.items() if not k.startswith(CLOCK_DEPENDENT)}
    return {
        "generator_version": GENERATOR_VERSION,
        "core": {t: d["rows"] for t, d in built.digests.items()},
        "dq": dq,
    }


def golden_core(built: SynthBuild) -> dict[str, Any]:
    """`generator_version` plus, per core table, the data columns, the row count and the
    digest of the sorted rows."""
    return {"generator_version": GENERATOR_VERSION, **built.digests}


def _dump(path: Path, value: object) -> None:
    text = json.dumps(value, indent=1, sort_keys=True) + "\n"
    path.write_text(text, encoding="utf-8", newline="\n")  # LF on every OS


def regenerate(root: Path, cfg_root: Path) -> None:
    """Generate seed 42 / small / dirty default into `root` (must not exist), build it and
    rewrite both goldens: `python -m tests.support.synth_build <root> <cfg_root>`."""
    generate(root)
    built = build_synth(root, cfg_root)
    _dump(GOLDEN_COUNTS, golden_counts(built))
    _dump(GOLDEN_CORE, golden_core(built))


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.stderr.write("usage: python -m tests.support.synth_build <root> <cfg_root>\n")
        sys.exit(2)
    regenerate(Path(sys.argv[1]), Path(sys.argv[2]))
