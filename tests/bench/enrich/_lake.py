"""The `full` synthetic lake build the pipeline/IO rows run on (T03-36; BT03-01, -07 ... -11).

`build_lake` runs the real spec 02 `build_pipeline` stage `build` (files 000-299: staging,
`core.*`) on the seed 42 lake inside the pinned fixture clock (`SynthEnv` as-of 2026-09-01,
`pinned_clock()`), exactly as `tests.support.metrics_synth.scored_build` does for its first
step, but stops there: the enrichment rows need `core.*` and the empty `enrich.*` DDL, not the
facts or the scoring. The root named by `HERNESS_SYNTH_FULL_ROOT` is reused and left as it was
generated (the warehouse folder is removed afterwards); without the variable the lake is
generated into the work folder. Enrichment state (cache, labels, vectors, snapshots) never
goes into the lake root: rows write to their own data roots under the work folder.
"""

from __future__ import annotations

import dataclasses
import os
import shutil
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import duckdb
import yaml
from tests.bench.enrich._common import FULL_ROOT_ENV, SEED, scale_label
from tests.support._build_clock import pinned_clock
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_synth import SynthEnv, generate_root, release, remove_builds

from herness.core import config as herness_config
from herness.core.config import HernessConfig
from herness.model._build_support import db_settings
from herness.model.build import run_build_pipeline
from herness.model.settings import BuildSettings
from herness.store import warehouse
from herness.store._warehouse_rw import open_for_build
from herness.store.ops import reset_connections
from herness.store.ops.migrate import migrate

__all__ = ["LakeBuild", "Report", "activate_enrich", "build_lake", "fill_text_sql", "scratch"]


@dataclasses.dataclass
class Report:
    """Stand-in for `StageReport`: the counters the stages under test add to."""

    rows: int = 0
    cache_hits: int = 0
    failed: int = 0
    decided: int = 0
    status: str = "done"
    note: str | None = None


@dataclasses.dataclass(frozen=True)
class LakeBuild:
    """The finished `core.*` build of the lake and its counts."""

    env: SynthEnv
    build_id: str
    path: Path
    work: Path
    build_s: float
    incidents: int
    changes: int
    problems: int
    services: int
    teams: int

    @property
    def scale(self) -> str:
        return scale_label(self.incidents)

    def open(self) -> duckdb.DuckDBPyConnection:
        """A writable connection of the build file (auto-commit); the caller closes it."""
        return self.open_id(self.build_id)

    def open_id(self, build_id: str) -> duckdb.DuckDBPyConnection:
        """A writable connection of build `build_id` with the PRODUCTION settings: the
        defaults of `sources.yaml: build` (memory limit 75 % of the RAM, all threads, spill
        folder, `preserve_insertion_order` off) through `db_settings`, as the pipeline's own
        build opens it. `SynthEnv.open_build` would cap memory at 16 GB (a harness value that
        made the BT03-09 range joins fail with an out-of-memory error when run alone)."""
        return open_for_build(
            build_id, create=False, cfg=db_settings(BuildSettings()), layout=self.env.layout
        )


def _counts(env: SynthEnv, build_id: str) -> dict[str, int]:
    con = warehouse.open_readonly(build_id, layout=env.layout)
    try:
        out = {}
        for table in ("incident", "change", "problem", "service", "team"):
            row = con.execute(f"SELECT count(*) FROM core.{table}").fetchone()  # noqa: S608
            out[table] = int(row[0]) if row else 0
        return out
    finally:
        con.close()


def build_lake(work: Path) -> tuple[LakeBuild, Any]:
    """Build the lake; return the build and the cleanup callable (idempotent)."""
    cached = os.environ.get(FULL_ROOT_ENV)
    root = Path(cached) if cached else generate_root(SEED, "full", work / "synth", workers=8)
    env = SynthEnv.create(root, work / "env")
    before = {p.name for p in env.data_root.iterdir()}

    def cleanup() -> None:
        remove_builds(env)
        for child in env.data_root.iterdir():  # anything else a stage wrote under the lake root
            if child.name not in before:
                shutil.rmtree(child, ignore_errors=True)
        if not cached:
            shutil.rmtree(root, ignore_errors=True)

    try:
        with pinned_clock():
            env.activate()
            start = time.perf_counter()
            outcome = run_build_pipeline(FakeJobContext({"stages": ["build"]}))
            build_s = time.perf_counter() - start
    except BaseException:
        cleanup()
        raise
    build_id = str(outcome.result["build_id"])
    c = _counts(env, build_id)
    path = warehouse.build_path(build_id, layout=env.layout)
    lake = LakeBuild(env, build_id, path, work, build_s, c["incident"], c["change"],
                     c["problem"], c["service"], c["team"])  # fmt: skip
    release()
    return lake, cleanup


def activate_enrich(
    lake: LakeBuild, data_root: Path, tag: str, *, decisions: dict[str, Any] | None = None,
    models: dict[str, Any] | None = None,
) -> HernessConfig:  # fmt: skip
    """Load a config derived from the lake's tree whose `paths.data` is `data_root` (so the
    cache, labels, vectors and snapshots of a row never touch the lake root), with the
    top-level keys of `decisions.yaml` / `models.yaml` replaced by the given overrides. Points
    the ops store at a fresh database of this row."""
    cfg_dir = lake.work / f"cfg-{tag}"
    if cfg_dir.exists():
        shutil.rmtree(cfg_dir)
    shutil.copytree(lake.env.cfg_dir, cfg_dir)
    herness = cfg_dir / "herness.yaml"  # the pinned OpenJev image `build_decider` parses
    image = "razorback16/openjev@sha256:" + "b" * 64
    herness.write_text(herness.read_text("utf-8").replace("<openjev-image>", image), "utf-8")
    for name, edits in (("decisions.yaml", decisions), ("models.yaml", models)):
        if edits:
            path = cfg_dir / name
            doc = yaml.safe_load(path.read_text(encoding="utf-8"))
            doc.update(edits)
            path.write_text(yaml.safe_dump(doc), encoding="utf-8")
    data_root.mkdir(parents=True, exist_ok=True)
    herness_config.reset_config()
    cfg = herness_config.init_config(
        "local", config_dir=cfg_dir, env={"HERNESS_PATHS__DATA": str(data_root)}
    )
    reset_connections(path=lake.work / f"ops-{tag}.sqlite")
    migrate()
    return cfg


def scratch(path: Path) -> Iterator[Path]:
    """A folder removed on exit (generator for fixtures)."""
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def fill_text_sql(schema_prefix: str = "") -> list[str]:
    """SQL statements filling `<prefix>enrich.text_redacted` for every record of `core.*`
    with the composed text (short description, blank line, body; trimmed) and
    `content_hash = left(sha256(text), 32)`. This is the unredacted stand-in of the text
    stage used to prepare large states quickly; the redaction speed itself is BT03-01."""
    sources = (
        ("incident", "core.incident", "short_description", "description"),
        ("change", "core.change", "short_description", "description"),
        ("problem", "core.problem", "NULL", "root_cause_text"),
    )
    out = []
    for entity, table, short, body in sources:
        out.append(
            f"INSERT INTO {schema_prefix}enrich.text_redacted (record_id, entity, text,"  # noqa: S608
            f" content_hash) SELECT record_id, '{entity}', t, left(sha256(t), 32) FROM ("
            f"SELECT record_id, trim(coalesce({short}, '') || E'\\n\\n' || coalesce({body}, ''))"
            f" AS t FROM {table} WHERE record_id IS NOT NULL) WHERE t <> ''"
        )
    return out
