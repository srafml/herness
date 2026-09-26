"""Nightly spot-check selection over ``enrich_resolved`` (U03-81, design 03 §5.7 step 5).

Private sibling of `herness.enrich.resolve` (T03-20 spec note, impl 03 §2): split off for the
380-line budget of ``resolve.py``, which re-exports `select_spot_checks` as its public name.
Payloads carry ids, hashes and scores only, never text (TH03-03).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from itertools import groupby
from typing import Final

import duckdb
import pyarrow as pa

from herness.core.errors import SchemaViolation
from herness.core.types import QuestionSet
from herness.enrich.questions import PAIR_QUESTIONS, question_fingerprint
from herness.enrich.settings import DecisionsConfig

__all__ = ["select_spot_checks"]

_META_SCHEMA: Final = pa.schema(
    [("question", pa.string()), ("threshold", pa.float64()), ("n_cap", pa.int64())]
)
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)
# n per question; candidates deduplicated per content hash (the review item match key) and
# ordered by sha256(build_id|content_hash|question); only rows a pick can come from return.
_SQL: Final = """
WITH new AS (
    SELECT * FROM enrich_resolved
    WHERE status = 'final' AND decider <> 'human' AND decided_at >= $since
),
wanted AS (
    SELECT m.question, m.threshold,
        least(m.n_cap, CAST(floor($rate * count(n.record_id)) AS BIGINT)) AS n
    FROM spot_meta AS m LEFT JOIN new AS n ON n.question = m.question
    GROUP BY m.question, m.threshold, m.n_cap
),
cand AS (
    SELECT n.question, w.n, n.record_id, n.content_hash, n.answer, n.probability, n.decider,
        n.decider_version,
        coalesce(n.probability BETWEEN w.threshold AND w.threshold + 0.1, false) AS in_band,
        sha256($build_id || '|' || n.content_hash || '|' || n.question) AS h
    FROM new AS n JOIN wanted AS w ON w.question = n.question AND w.n > 0
    QUALIFY row_number() OVER (
        PARTITION BY n.question, n.content_hash ORDER BY n.record_id, n.entity) = 1
),
ranked AS (
    SELECT *, row_number() OVER (PARTITION BY question ORDER BY h) AS u_rank,
        row_number() OVER (PARTITION BY question, in_band ORDER BY h) AS b_rank
    FROM cand
)
SELECT question, n, u_rank, in_band, record_id, content_hash, answer, probability, decider,
    decider_version
FROM ranked WHERE u_rank <= n OR (in_band AND b_rank <= n)
ORDER BY question, h
"""
type _Row = tuple[str, int, int, bool, str, str, str, float, str, str]


def _pick(rows: list[_Row], n: int) -> list[_Row]:
    """``n // 2`` uniform picks, then band picks, then uniform fill; ``rows`` are hash-ordered.

    ``rows`` hold the first ``n`` rows by hash (``u_rank <= n``) plus the first ``n`` band
    rows, which is enough for every branch.
    """
    chosen = [r for r in rows if r[2] <= n // 2]
    band = [r for r in rows if r[3] and r not in chosen]
    chosen += band[: n - n // 2]
    return chosen + [r for r in rows if r[2] <= n and r not in chosen][: n - len(chosen)]


def _meta(qs: QuestionSet, cfg: DecisionsConfig, open_counts: Mapping[str, int]) -> pa.Table:
    """(question, threshold, n_cap) per non-pair question; n_cap applies both fixed caps."""
    spot = cfg.spot_check
    rows = [
        {
            "question": q.id,
            "threshold": q.threshold,
            "n_cap": min(
                spot.nightly_max_per_question,
                max(0, spot.open_cap_per_question - open_counts.get(q.id, 0)),
            ),
        }
        for q in qs.questions
        if q.id not in PAIR_QUESTIONS
    ]
    return pa.Table.from_pylist(rows, schema=_META_SCHEMA)


def select_spot_checks(
    wh: duckdb.DuckDBPyConnection,
    *,
    qs: QuestionSet,
    cfg: DecisionsConfig,
    build_id: str,
    since: datetime,
    open_counts: Mapping[str, int],
) -> list[dict[str, object]]:
    """``label_check`` payloads for the nightly spot-checks, per question in set order (U03-81).

    ``n = min(nightly_max_per_question, floor(nightly_rate x N_new), max(0, open cap - open))``
    where ``N_new`` counts the final non-human rows decided at or after ``since``; the picks
    are deterministic for a ``build_id``. Raises SchemaViolation on a DuckDB error (for
    example a missing ``enrich_resolved``).
    """
    params = {"since": since, "rate": cfg.spot_check.nightly_rate, "build_id": build_id}
    wh.register("spot_meta", _meta(qs, cfg, open_counts))
    try:
        rows: list[_Row] = wh.execute(_SQL, params).fetchall()
    except duckdb.Error as exc:
        schema = isinstance(exc, _SCHEMA_ERRORS)
        reason = str(exc).splitlines()[0] if schema else type(exc).__name__
        msg = f"select_spot_checks: {reason}"
        raise SchemaViolation(msg) from exc
    finally:
        wh.unregister("spot_meta")
    groups = {qid: list(group) for qid, group in groupby(rows, key=lambda r: r[0])}
    payloads: list[dict[str, object]] = []
    for q in qs.questions:
        group = groups.get(q.id)
        if not group:
            continue
        fingerprint = q.fingerprint or question_fingerprint(q)
        payloads += [
            {
                "record_id": r[4], "content_hash": r[5], "question": q.id,
                "question_fingerprint": fingerprint, "question_set_version": qs.version,
                "answer": r[6], "probability": r[7], "decider": r[8], "decider_version": r[9],
                "purpose": "spot_check", "text_ref": "enrich.text_redacted",
            }
            for r in _pick(group, group[0][1])
        ]  # fmt: skip
    return payloads
