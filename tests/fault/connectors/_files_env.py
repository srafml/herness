"""Shared set-up of the files-connector fault tests (impl 01 FT01-02, FT01-07; T01-13).

The data root holds the committed `lake_small` raw lake (every `core.*` table gets rows)
plus whatever the files connector writes under `raw/files/`. The config tree is the full
test tree with the `lake_small` mappings, two build threads and the files source with one
delta entity `teams` (the `tests.support.sync_env` inbox stand-in). `build` runs the real
`build_pipeline` stage `build` over that lake and opens the build file read-only.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import FIXTURE
from tests.support.sync_env import FILES_SOURCES_YAML, write_sync_config

from herness.core import config as c
from herness.core.types import JobOutcome
from herness.model.build import run_build_pipeline
from herness.store import warehouse
from herness.store.layout import DataLayout

SOURCES_YAML = FILES_SOURCES_YAML.replace("version: 1\n", "version: 1\nbuild:\n  threads: 2\n")
TEAMS_RAW = ("raw", "files", "teams")


def write_config(root: Path) -> Path:
    """Write the config tree under `root` (paths.data = `root/data`); return its dir."""
    mappings = (FIXTURE / "mappings.yaml").read_text(encoding="utf-8")
    return write_sync_config(root, SOURCES_YAML, mappings)


def load(config_dir: Path) -> c.HernessConfig:
    """Load and cache the tree for `get_config` in this process."""
    c.reset_config()
    return c.init_config("local", config_dir=config_dir, env={})


def committed_files(data_root: Path) -> list[Path]:
    """The committed Parquet files of `files/teams` (dot-prefixed temp files excluded)."""
    root = data_root.joinpath(*TEAMS_RAW)
    return sorted(p for p in root.rglob("*.parquet") if not p.name.startswith("."))


def lake_table(files: list[Path]) -> pa.Table:
    """All rows of `files` as one table (the raw lake, before any staging dedupe)."""
    return pa.concat_tables([pq.read_table(path) for path in files], promote_options="default")


def build() -> JobOutcome:
    """Run `build_pipeline` stage `build` over the lake of the loaded config."""
    outcome = run_build_pipeline(FakeJobContext({"stages": ["build"]}))
    assert outcome.status == "done", outcome
    return outcome


@contextlib.contextmanager
def open_build(data_root: Path, build_id: str) -> Iterator[duckdb.DuckDBPyConnection]:
    """The finished build file, read-only."""
    with warehouse.open_readonly(build_id, layout=DataLayout.from_root(data_root)) as con:
        yield con


def core_duplicates(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """Per `core.*` table: rows minus distinct rows (0 everywhere means no duplicates)."""
    tables = con.execute(
        "SELECT table_name FROM duckdb_tables() WHERE schema_name = 'core' ORDER BY table_name"
    ).fetchall()
    out: dict[str, int] = {}
    for (table,) in tables:
        query = (
            f'SELECT (SELECT count(*) FROM core."{table}")'  # noqa: S608 - catalog names
            f' - (SELECT count(*) FROM (SELECT DISTINCT * FROM core."{table}"))'
        )
        row = con.execute(query).fetchone()
        assert row is not None
        out[f"core.{table}"] = int(row[0])
    return out


def staged_teams(con: duckdb.DuckDBPyConnection) -> list[tuple[str, str]]:
    """`(record_id, source_key)` of every `stg.files_teams` row, sorted."""
    rows = con.execute(
        "SELECT record_id, source_key FROM stg.files_teams ORDER BY record_id"
    ).fetchall()
    return [(str(a), str(b)) for a, b in rows]
