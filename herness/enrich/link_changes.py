"""Incident<->change linking: heuristic, candidate SQL, pair inputs, link stage (U03-107..110).

Design 03 §5.10. ``link_candidates`` runs ``sql/link_candidates.sql`` into the temp table
``link_cand``; ``pair_inputs`` builds ``change_caused_pair`` items for window candidates in the
decider band and writes the pair index; ``run_link_stage`` blends cached, calibrated pair answers
into the heuristic score and writes ``enrich.incident_change_link``. Pair decisions are never
written to ``enrich.decision`` (open item OI-08).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterator
from datetime import datetime
from importlib import resources
from typing import Final, Protocol

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.dataset as ds
import pyarrow.parquet as pq

from herness.core.errors import ConfigError, SchemaViolation
from herness.core.logging import get_logger
from herness.core.resilience.metrics import record_counter
from herness.core.types import DecisionInput, QuestionSet
from herness.enrich.cache import DecisionCache, replace_atomic
from herness.enrich.calibrate import CalibrationStore, apply_temperature
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import question_fingerprint
from herness.enrich.settings import DecisionsConfig
from herness.enrich.text import content_hash, pair_text

__all__ = ["heuristic_link_score", "link_candidates", "pair_inputs", "run_link_stage"]

PAIR_QUESTION: Final = "change_caused_pair"
METHODS: Final = ("source_field", "time_ci_window", "decider")
_BOOSTED_OUTCOMES: Final = frozenset({"unsuccessful", "backed_out", "successful_with_issues"})
_OUTCOME_BOOST: Final = 1.25
_EMERGENCY_BOOST: Final = 1.1
_SIDE_MAX_CHARS: Final = 5_990  # two sides + pair_text's 20 framing chars <= DecisionInput 12,000
_BUILD_ID_RE: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,95}$")
_INDEX_SCHEMA: Final = pa.schema(
    [("incident_id", pa.string()), ("change_id", pa.string()), ("content_hash", pa.string())]
)
_SQL_PARAMS: Final = ("before_h", "after_h", "tau_h", "ci_weight", "service_weight", "min_score",
                      "top_n")  # fmt: skip
_DECIDED_VIEW: Final = "_link_decided"
_DECIDED_SCHEMA: Final = pa.schema(
    [("incident_id", pa.string()), ("change_id", pa.string()), ("score", pa.float64())]
)
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)

_log = get_logger("enrich.link")


class _Report(Protocol):
    """Counters the stage mutates.

    T03-xx pipeline: retype to StageReport (U03-142, herness.enrich.pipeline).
    """

    rows: int
    decided: int


# --- U03-107 -----------------------------------------------------------------------------------


def heuristic_link_score(  # noqa: PLR0913 - U03-107's keyword-only signature is binding (8 params)
    *,
    same_ci: bool,
    same_service: bool,
    delta_h: float,
    outcome: str | None,
    change_type: str | None,
    tau_h: float,
    ci_weight: float,
    service_weight: float,
) -> float:
    """Time/CI window score (U03-107, design 03 §5.10 step 2); reference for the SQL.

    ``min(1, m * exp(-max(delta_h, 0) / tau_h) * b)``. Raises ConfigError when neither flag
    is set.
    """
    if not (same_ci or same_service):
        msg = "a window link needs the same ci_id or the same service_id"
        raise ConfigError(msg)
    m = ci_weight if same_ci else service_weight
    b = _OUTCOME_BOOST if outcome in _BOOSTED_OUTCOMES else 1.0
    if change_type == "emergency":
        b *= _EMERGENCY_BOOST
    return min(1.0, m * math.exp(-max(delta_h, 0.0) / tau_h) * b)


# --- U03-108 -----------------------------------------------------------------------------------


def _duck_error(exc: duckdb.Error, *, stage: str) -> SchemaViolation:
    """SchemaViolation naming only the catalog/binder message or the class (never row values)."""
    schema_error = isinstance(exc, _SCHEMA_ERRORS) and str(exc)
    reason = str(exc).splitlines()[0] if schema_error else type(exc).__name__
    return SchemaViolation(f"link stage {stage}: {reason}")


def link_candidates(wh: duckdb.DuckDBPyConnection, *, cfg: DecisionsConfig) -> None:
    """Run ``sql/link_candidates.sql`` into the temp table ``link_cand`` (U03-108)."""
    sql = resources.files("herness.enrich").joinpath("sql/link_candidates.sql").read_text("utf-8")
    params = cfg.change_link.model_dump(include=set(_SQL_PARAMS))
    try:
        wh.execute(sql, params)
    except duckdb.Error as exc:
        raise _duck_error(exc, stage="candidates") from exc


# --- U03-109 -----------------------------------------------------------------------------------


def _band_pairs(
    wh: duckdb.DuckDBPyConnection, cfg: DecisionsConfig, *, limit: int | None
) -> Iterator[tuple[str, str, float, str]]:
    """``(incident_id, change_id, heuristic, pair text)`` of window pairs in the decider band.

    Ordered by incident ``opened_at DESC``, ``incident_id``, ``change_id``; pairs without both
    ``text_redacted`` rows are skipped (inner joins).
    """
    low, high = cfg.change_link.decider_band
    sql = """
        SELECT l.incident_id, l.change_id, l.score, ti.text, tc.text
        FROM link_cand AS l
        JOIN core.incident AS i ON i.record_id = l.incident_id
        JOIN enrich.text_redacted AS ti ON ti.entity = 'incident' AND ti.record_id = l.incident_id
        JOIN enrich.text_redacted AS tc ON tc.entity = 'change' AND tc.record_id = l.change_id
        WHERE l.method = 'time_ci_window' AND l.score BETWEEN ? AND ?
        ORDER BY i.opened_at DESC NULLS LAST, l.incident_id, l.change_id
    """
    params: list[object] = [low, high]
    if limit is not None:
        sql += " LIMIT ?"
        params.append(limit)
    try:
        rows = wh.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        raise _duck_error(exc, stage="band pairs") from exc
    for incident_id, change_id, score, incident_text, change_text in rows:
        text = pair_text(incident_text[:_SIDE_MAX_CHARS], change_text[:_SIDE_MAX_CHARS])
        yield incident_id, change_id, float(score), text


def _valid_build_id(build_id: str) -> str:
    if _BUILD_ID_RE.fullmatch(build_id) is None or build_id.endswith("."):
        msg = "invalid build_id for the pair index"
        raise ConfigError(msg)
    return build_id


def pair_inputs(
    wh: duckdb.DuckDBPyConnection,
    *,
    cfg: DecisionsConfig,
    qs: QuestionSet,
    paths: EnrichPaths,
    build_id: str,
) -> list[DecisionInput]:
    """``change_caused_pair`` inputs for band pairs; writes the pair index (U03-109).

    Returns ``[]`` (and writes nothing) when ``change_link.use_decider`` is off. Raises
    ConfigError when ``qs`` lacks ``change_caused_pair`` or ``build_id`` is not a safe name.
    """
    if not cfg.change_link.use_decider:
        return []
    qs.get(PAIR_QUESTION)
    target = paths.pairs_dir() / f"part-{_valid_build_id(build_id)}.parquet"
    inputs: list[DecisionInput] = []
    rows: list[dict[str, str]] = []
    for incident_id, change_id, _, text in _band_pairs(
        wh, cfg, limit=cfg.change_link.decider_max_pairs
    ):
        digest = content_hash(text)
        record_id = f"{incident_id}|{change_id}"
        inputs.append(DecisionInput(record_id=record_id, entity="incident", content_hash=digest,
                                    text=text, question_ids=(PAIR_QUESTION,)))  # fmt: skip
        rows.append({"incident_id": incident_id, "change_id": change_id, "content_hash": digest})
    index = pa.Table.from_pylist(rows, schema=_INDEX_SCHEMA)
    replace_atomic(
        target, lambda tmp: pq.write_table(index, tmp, compression="zstd"), kind="pair index"
    )
    return inputs


# --- U03-110 -----------------------------------------------------------------------------------


def _pair_p_true(
    cache: DecisionCache, qs: QuestionSet, pair_decider: tuple[str, str]
) -> dict[str, float]:
    """Raw ``P(true)`` of the latest current-fingerprint pair answer per content hash."""
    dataset = cache.dataset()
    if dataset is None:
        return {}
    question = qs.get(PAIR_QUESTION)
    decider, version = pair_decider
    condition = (
        (ds.field("decider") == decider)
        & (ds.field("decider_version") == version)
        & (ds.field("question") == PAIR_QUESTION)
        & (
            ds.field("question_fingerprint")
            == (question.fingerprint or question_fingerprint(question))
        )
    )
    table = dataset.to_table(
        columns=["content_hash", "distribution", "decided_at"], filter=condition
    )
    latest: dict[str, tuple[datetime, float]] = {}
    for digest, dist, decided_at in zip(
        table.column("content_hash").to_pylist(),
        table.column("distribution").to_pylist(),
        table.column("decided_at").to_pylist(),
        strict=True,
    ):
        p_true = dict(dist).get("true", 0.0)
        kept = latest.get(digest)
        if kept is None or decided_at > kept[0]:
            latest[digest] = (decided_at, p_true)
    return {digest: p for digest, (_, p) in latest.items()}


def _decided_rows(
    wh: duckdb.DuckDBPyConnection,
    cfg: DecisionsConfig,
    qs: QuestionSet,
    cache: DecisionCache,
    calibration: CalibrationStore,
    pair_decider: tuple[str, str],
) -> pa.Table:
    """``(incident_id, change_id, score)`` of band pairs with a cached pair answer."""
    p_true = _pair_p_true(cache, qs, pair_decider)
    hits = [
        (incident_id, change_id, heuristic, p_true[digest])
        for incident_id, change_id, heuristic, text in _band_pairs(wh, cfg, limit=None)
        if (digest := content_hash(text)) in p_true
    ]
    t, _uncalibrated = calibration.temperature(*pair_decider, qs.version, PAIR_QUESTION)
    raw = np.array([[p, 1.0 - p] for *_, p in hits], dtype=np.float64).reshape(-1, 2)
    calibrated = apply_temperature(raw, t, "bool")[:, 0] if hits else np.zeros(0)
    return pa.table(
        {
            "incident_id": [h[0] for h in hits],
            "change_id": [h[1] for h in hits],
            "score": [0.5 * h[2] + 0.5 * float(p) for h, p in zip(hits, calibrated, strict=True)],
        },
        schema=_DECIDED_SCHEMA,
    )


_INSERT_SQL: Final = f"""
    INSERT INTO enrich.incident_change_link (incident_id, change_id, method, score)
    SELECT l.incident_id, l.change_id,
        CASE WHEN d.change_id IS NULL THEN l.method ELSE 'decider' END AS method,
        coalesce(d.score, l.score) AS score
    FROM link_cand AS l
    LEFT JOIN {_DECIDED_VIEW} AS d
        ON d.incident_id = l.incident_id AND d.change_id = l.change_id
            AND l.method = 'time_ci_window'
    QUALIFY row_number() OVER (
        PARTITION BY l.incident_id
        ORDER BY l.method = 'source_field' DESC, coalesce(d.score, l.score) DESC, l.change_id
    ) <= ?
    RETURNING method
"""  # noqa: S608 - the only interpolated name is the fixed _DECIDED_VIEW constant


def run_link_stage(  # noqa: PLR0913 - U03-110's signature is binding (wh + 6 keyword-only)
    wh: duckdb.DuckDBPyConnection,
    *,
    cfg: DecisionsConfig,
    qs: QuestionSet,
    cache: DecisionCache,
    calibration: CalibrationStore,
    pair_decider: tuple[str, str] | None,
    report: _Report,
) -> None:
    """Write ``enrich.incident_change_link`` from ``link_cand`` and cached pair answers (U03-110).

    Band pairs with a current ``change_caused_pair`` answer of ``pair_decider`` become
    ``method='decider'`` with ``score = 0.5 * heuristic + 0.5 * p'(true)``; source-field rows
    rank first per incident; at most ``top_n`` rows per incident. Insert only: expects the
    fresh, empty ``enrich.incident_change_link`` of this build's warehouse (one run per build).
    """
    decided = (
        _decided_rows(wh, cfg, qs, cache, calibration, pair_decider)
        if cfg.change_link.use_decider and pair_decider is not None
        else _DECIDED_SCHEMA.empty_table()
    )
    wh.register(_DECIDED_VIEW, decided)
    try:
        methods = [row[0] for row in wh.execute(_INSERT_SQL, [cfg.change_link.top_n]).fetchall()]
    except duckdb.Error as exc:
        raise _duck_error(exc, stage="insert") from exc
    finally:
        wh.unregister(_DECIDED_VIEW)
    counts = {method: methods.count(method) for method in METHODS}
    for method, n in counts.items():
        record_counter(
            "herness_enrich_links_total", n, component="enrich", labels={"method": method}
        )
    report.rows += len(methods)
    report.decided += counts["decider"]
    _log.info("enrich.link.completed", **counts)
