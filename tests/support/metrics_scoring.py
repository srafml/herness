"""Scoring test helpers (impl 04 T04-13): config patches, a small catalog and a tiny build file.

`run_scoring` reads `get_config()`, `catalog_from_config(cfg)` and `config_hash(cfg)`; the
tests point those names of `herness.metrics.scoring` at the `metrics_tiny` weights, a chosen
catalog and a fixed hash. `patches` returns (object, attribute, value) triples so the pytest
tests (monkeypatch) and the FT04-01 child process (plain setattr) apply the same ones.
"""

import functools
from pathlib import Path
from types import SimpleNamespace
from typing import Final

import duckdb
from freezegun import freeze_time
from tests.support.metrics_tiny import BUILD_ID, build_metrics_tiny, shipped_catalog, tiny_weights

from herness.core.config_view import ConfigIssue
from herness.metrics import _scoring_checks, facts, scoring
from herness.metrics.catalog import MetricCatalog, validate_catalog
from herness.metrics.facts import materialize_facts
from herness.metrics.settings import MetricsCatalogConfig, WeightsConfig

CFG_HASH: Final = "cfg_00000000000000aa"
# One metric per unit family the checks inspect; the scorecard keeps only enabled metrics.
SMALL_METRICS: Final = frozenset({"change_failure_rate", "incident_count", "mtta_minutes"})
FACTS_TIME: Final = "2026-04-01 06:30:00"
_WEIGHTS: Final = tiny_weights()


@functools.cache
def small_catalog() -> MetricCatalog:
    """The shipped catalog with only `SMALL_METRICS` enabled (one object per process)."""
    keep = SMALL_METRICS
    cfg = shipped_catalog().config
    metrics = [m.model_copy(update={"enabled": m.name in keep}) for m in cfg.metrics]
    org = cfg.scoring.org
    weights = {k: v for k, v in org.metrics.items() if k in keep}
    scoring_cfg = cfg.scoring.model_copy(
        update={"org": org.model_copy(update={"metrics": weights})}
    )
    return MetricCatalog(cfg.model_copy(update={"metrics": metrics, "scoring": scoring_cfg}))


_VALIDATED: dict[tuple[int, int], tuple[object, list[ConfigIssue]]] = {}


def _validate_once(cfg: MetricsCatalogConfig, /, *, weights: WeightsConfig) -> list[ConfigIssue]:
    """`validate_catalog` memoized per (catalog, weights) object: it renders every metric."""
    key = (id(cfg), id(weights))
    if key not in _VALIDATED:
        _VALIDATED[key] = ((cfg, weights), validate_catalog(cfg, weights=weights))
    return list(_VALIDATED[key][1])


def patches(catalog: MetricCatalog, cfg_hash: str = CFG_HASH) -> list[tuple[object, str, object]]:
    """(object, name, value) triples pointing facts and scoring at the tiny config."""
    cfg = SimpleNamespace(weights=_WEIGHTS, metrics=catalog.config)
    return [
        (scoring, "validate_catalog", _validate_once),
        (facts, "catalog_from_config", shipped_catalog),
        (facts, "get_config", lambda: cfg),
        (scoring, "get_config", lambda: cfg),
        (scoring, "catalog_from_config", lambda _cfg=None: catalog),
        (scoring, "config_hash", lambda _cfg: cfg_hash),
        (_scoring_checks, "get_config", lambda: cfg),
        (_scoring_checks, "config_hash", lambda _cfg: cfg_hash),
    ]


def tiny_with_facts() -> duckdb.DuckDBPyConnection:
    """`metrics_tiny` with the stage 400 fact tables (facts must already be patched)."""
    con = build_metrics_tiny()
    with freeze_time(FACTS_TIME):
        materialize_facts(con, BUILD_ID)
    return con


def save_as_file(con: duckdb.DuckDBPyConnection, path: Path) -> None:
    """Copy the in-memory database of `con` into a new DuckDB file at `path`."""
    literal = "'" + str(path).replace("'", "''") + "'"
    con.execute(f"ATTACH {literal} AS target")
    try:
        con.execute("COPY FROM DATABASE memory TO target")
    finally:
        con.execute("DETACH target")
