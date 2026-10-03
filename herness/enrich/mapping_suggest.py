"""Mapping suggestions: name normalization, scores, in-memory vectors, stage `suggest`.

U03-111 ... U03-114, design 03 §5.11. Unmapped Jira components and teams are scored against
every service; the best become `pending` `mapping_suggestion` items. Nothing is approved and
`core.service_map` is never written here (TH03-10). Encoder texts are redacted (TH03-02);
payloads and logs carry ids, scores and counts only (TH03-03); SQL values are bound parameters.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Final, Literal

import duckdb
import numpy as np
from rapidfuzz import fuzz, process

from herness.core import time as clock
from herness.core.errors import SchemaViolation
from herness.core.logging import get_logger
from herness.core.redact import get_redactor
from herness.core.resilience.metrics import record_counter
from herness.core.types import QuestionSet
from herness.enrich.embed import Encoder, embed_texts
from herness.enrich.review_items import create_if_absent
from herness.enrich.settings import DecisionsConfig, MappingWeights
from herness.enrich.text import normalize_text

if TYPE_CHECKING:
    from herness.enrich.pipeline import StageReport as _Report

__all__ = [
    "ALGORITHM_VERSION", "MappingVectors", "ScoreMatrices", "Service", "Subject", "SubjectType",
    "mapping_scores", "norm_name", "prepare_mapping_vectors", "run_suggest_stage",
]  # fmt: skip

type SubjectType = Literal["jira_component", "team"]
type _Rows = list[tuple[Any, ...]]

ALGORITHM_VERSION: Final = "map-v1"
MAX_SUBJECTS, MAX_SERVICES, MAX_OPTIONS = 2_000, 5_000, 1_000  # U03-113 limits
_RECENT, _SNIPPET_CHARS, _BATCH, _ROUND = 20, 200, 128, 6
_MATCH_KEYS: Final = ("subject_type", "jira_project", "jira_component", "team_id", "service_id")
_SCORES: Final = ("score", "fuzzy", "semantic", "cooccurrence")

_JIRA_WHERE: Final = "service_id IS NULL AND component IS NOT NULL AND project IS NOT NULL"
_JIRA_SQL: Final = f"""
SELECT project, component, count(*) AS n FROM core.work_item WHERE {_JIRA_WHERE}
GROUP BY project, component ORDER BY n DESC, project, component LIMIT $limit
"""  # noqa: S608 - module constants only
_SUMMARY_SQL: Final = f"""
SELECT project, component, summary FROM core.work_item
WHERE {_JIRA_WHERE} AND summary IS NOT NULL
QUALIFY row_number() OVER (PARTITION BY project, component
    ORDER BY created_at DESC NULLS LAST, record_id) <= $recent
ORDER BY project, component, created_at DESC NULLS LAST, record_id
"""  # noqa: S608 - module constants only
_TEAM_SQL: Final = """
SELECT t.team_id, t.name FROM core.team AS t
WHERE t.team_id IS NOT NULL AND coalesce(t.active, true)
    AND NOT EXISTS (SELECT 1 FROM core.service_map AS m WHERE m.team_id = t.team_id)
ORDER BY t.team_id LIMIT $limit
"""
_SERVICE_SQL: Final = """
SELECT service_id, name FROM core.service WHERE service_id IS NOT NULL
ORDER BY service_id LIMIT $limit
"""
# `{col}` is only ever the constant "team_id" or "service_id", never caller data.
_SNIPPET_SQL: Final = """
SELECT i.{col}, left(t.text, $chars) FROM core.incident AS i
JOIN enrich.text_redacted AS t ON t.record_id = i.record_id AND t.entity = 'incident'
WHERE list_contains(CAST($ids AS VARCHAR[]), i.{col})
QUALIFY row_number() OVER (PARTITION BY i.{col}
    ORDER BY i.opened_at DESC NULLS LAST, i.record_id) <= $recent
ORDER BY i.{col}, i.opened_at DESC NULLS LAST, i.record_id
"""
_COOC_SQL: Final = """
SELECT team_id, service_id, count(*) FROM core.incident
WHERE list_contains(CAST($ids AS VARCHAR[]), team_id) GROUP BY team_id, service_id
"""

_log = get_logger("enrich.suggest")


@dataclass(frozen=True, slots=True)
class Subject:
    """One unmapped Jira (project, component) or team; `name` is used for fuzzy matching."""

    subject_type: SubjectType
    jira_project: str | None
    jira_component: str | None
    team_id: str | None
    name: str
    work_items: int = 0


@dataclass(frozen=True, slots=True)
class Service:
    """One `core.service` candidate."""

    service_id: str
    name: str


@dataclass(frozen=True, slots=True, eq=False)
class MappingVectors:
    """Subjects, services and their unit-norm vectors; never persisted (U03-113)."""

    subjects: tuple[Subject, ...]
    services: tuple[Service, ...]
    subject_vecs: np.ndarray
    service_vecs: np.ndarray
    option_vecs: dict[str, dict[str, np.ndarray]] = field(default_factory=dict)


@dataclass(frozen=True, slots=True, eq=False)
class ScoreMatrices:
    """Per (subject, service) component and combined scores, each (s, v) float32 (U03-112)."""

    fuzzy: np.ndarray
    semantic: np.ndarray
    cooccurrence: np.ndarray
    score: np.ndarray


def _clean(s: str) -> str:
    """Lower-case, every Unicode `P*` character to a space, whitespace collapsed."""
    chars = (" " if unicodedata.category(c).startswith("P") else c for c in s.lower())
    return " ".join("".join(chars).split())


def norm_name(s: str, *, abbreviations: Mapping[str, str]) -> str:
    """Normalize a name for fuzzy matching (U03-111, design 03 §5.11); expansions are cleaned
    too, so the result is idempotent when no expansion token is an abbreviation key."""
    return _clean(" ".join(abbreviations.get(t, t) for t in _clean(s).split()))


def mapping_scores(  # noqa: PLR0913 - U03-112's keyword-only signature is binding
    *, subject_names: Sequence[str], subject_types: Sequence[SubjectType],
    service_names: Sequence[str], subject_vecs: np.ndarray, service_vecs: np.ndarray,
    cooccurrence: np.ndarray, weights: MappingWeights, abbreviations: Mapping[str, str],
) -> ScoreMatrices:  # fmt: skip
    """Score every subject against every service; all values in [0, 1] (U03-112).

    Team rows: `w_f·fuzzy + w_s·semantic + w_c·cooc`; Jira rows move the co-occurrence
    weight to semantic. Vectors must be unit-norm.
    """
    shape = (len(subject_names), len(service_names))
    if 0 in shape:
        empty = np.zeros(shape, dtype=np.float32)
        return ScoreMatrices(empty, empty.copy(), empty.copy(), empty.copy())
    fuzzy = process.cdist(
        [norm_name(n, abbreviations=abbreviations) for n in subject_names],
        [norm_name(n, abbreviations=abbreviations) for n in service_names],
        scorer=fuzz.token_set_ratio, workers=-1, dtype=np.float32,
    ) / np.float32(100)  # fmt: skip
    semantic = np.clip(subject_vecs @ service_vecs.T, 0, 1).astype(np.float32)
    cooc = np.asarray(cooccurrence, dtype=np.float32)
    w_f, w_s, w_c = weights.fuzzy, weights.semantic, weights.cooccurrence
    team = np.asarray([t == "team" for t in subject_types], dtype=bool)[:, None]
    score = np.where(
        team, w_f * fuzzy + w_s * semantic + w_c * cooc, w_f * fuzzy + (w_s + w_c) * semantic
    )
    clipped = np.clip(score, 0, 1).astype(np.float32)
    return ScoreMatrices(np.clip(fuzzy, 0, 1).astype(np.float32), semantic, cooc, clipped)


def _fetch(wh: duckdb.DuckDBPyConnection, sql: str, params: dict[str, object], where: str
           ) -> _Rows:  # fmt: skip
    """Rows of `sql`; SchemaViolation naming only the DuckDB error class (never values)."""
    try:
        rows: _Rows = wh.execute(sql, params).fetchall()
    except duckdb.Error as exc:
        msg = f"mapping_suggest {where}: {type(exc).__name__}"
        raise SchemaViolation(msg) from exc
    return rows


def _capped[T](rows: list[T], limit: int, what: str) -> list[T]:
    """The first `limit` rows; logs a warning with the count when more were found."""
    if len(rows) > limit:
        _log.warning("enrich.suggest.capped", what=what, found=len(rows), limit=limit)
    return rows[:limit]


def _redact(groups: list[list[str]]) -> list[list[str]]:
    """Redact every string through T10-10's `redact_batch`; a failed item becomes `""`."""
    flat = [text for group in groups for text in group]
    if not flat:
        return [[] for _ in groups]
    out = iter(get_redactor().redact_batch(flat))
    return [[next(out) or "" for _ in group] for group in groups]


def _snippets(wh: duckdb.DuckDBPyConnection, col: str, ids: list[str]) -> dict[str, list[str]]:
    """First 200 chars of the 20 most recent redacted incident texts per `col` value."""
    by_key: dict[str, list[str]] = {}
    params: dict[str, object] = {"ids": ids, "chars": _SNIPPET_CHARS, "recent": _RECENT}
    for key, text in _fetch(wh, _SNIPPET_SQL.format(col=col), params, "snippets") if ids else []:
        by_key.setdefault(str(key), []).append(str(text or ""))
    return by_key


def _jira_subjects(wh: duckdb.DuckDBPyConnection) -> tuple[list[Subject], list[str]]:
    rows = _fetch(wh, _JIRA_SQL, {"limit": MAX_SUBJECTS + 1}, "jira subjects")
    summaries: dict[tuple[str, str], list[str]] = {}
    for project, component, summary in _fetch(wh, _SUMMARY_SQL, {"recent": _RECENT}, "summaries"):
        summaries.setdefault((project, component), []).append(str(summary))
    subjects = [Subject("jira_component", p, c, None, f"{p} {c}", int(n)) for p, c, n in rows]
    groups = [[s.name, *summaries.get((str(s.jira_project), str(s.jira_component)), [])]
              for s in subjects]  # fmt: skip
    return subjects, [f"{g[0]}: " + "; ".join(x for x in g[1:] if x) for g in _redact(groups)]


def _named_texts(names: list[str], keys: list[str], snippets: Mapping[str, list[str]]
                 ) -> list[str]:  # fmt: skip
    """`"<redacted name>: " + "; ".join(snippets)` per key (snippets are already redacted)."""
    red = _redact([[n] for n in names])
    return [f"{r[0]}: " + "; ".join(snippets.get(k, [])) for r, k in zip(red, keys, strict=True)]


def _team_subjects(wh: duckdb.DuckDBPyConnection) -> tuple[list[Subject], list[str]]:
    rows = _fetch(wh, _TEAM_SQL, {"limit": MAX_SUBJECTS + 1}, "teams")
    subjects = [Subject("team", None, None, str(t), str(n or "")) for t, n in rows]
    ids = [str(s.team_id) for s in subjects]
    return subjects, _named_texts([s.name for s in subjects], ids, _snippets(wh, "team_id", ids))


def _services(wh: duckdb.DuckDBPyConnection) -> tuple[list[Service], list[str]]:
    rows = _fetch(wh, _SERVICE_SQL, {"limit": MAX_SERVICES + 1}, "services")
    services = _capped([Service(str(s), str(n or "")) for s, n in rows], MAX_SERVICES, "services")
    ids = [s.service_id for s in services]
    names = [s.name for s in services]
    return services, _named_texts(names, ids, _snippets(wh, "service_id", ids))


def _option_texts(qs: QuestionSet) -> tuple[list[tuple[str, str]], list[str]]:
    """(question, label) keys and `"<label>: <redacted description>"` of dynamic options."""
    keys = [
        (q.id, label)
        for q in qs.questions
        if q.type == "choice" and q.options_source != "static" and q.options
        for label in sorted(q.options)
    ]
    keys = _capped(keys, MAX_OPTIONS, "options")
    options = {q.id: q.options or {} for q in qs.questions}
    red = _redact([[options[qid][label]] for qid, label in keys])
    return keys, [f"{label}: {r[0]}" for (_, label), r in zip(keys, red, strict=True)]


def prepare_mapping_vectors(
    wh: duckdb.DuckDBPyConnection, *, encoder: Encoder, qs: QuestionSet
) -> MappingVectors:
    """Subjects, services and in-memory vectors for stage `suggest` (U03-113).

    Runs in stage `embed` while `encoder` is loaded; texts are normalized (4,000 chars max).
    Inputs are cut, with a warning, to 2,000 subjects (Jira first), 5,000 services and 1,000
    options. Raises SchemaViolation on a DuckDB error; encoder errors propagate.
    """
    jira, jira_texts = _jira_subjects(wh)
    teams, team_texts = _team_subjects(wh)
    subjects = _capped(jira + teams, MAX_SUBJECTS, "subjects")
    subject_texts = (jira_texts + team_texts)[: len(subjects)]
    services, service_texts = _services(wh)
    option_keys, option_texts = _option_texts(qs)
    texts = [normalize_text(t) for t in (*subject_texts, *service_texts, *option_texts)]
    vecs = embed_texts(encoder, texts, batch_size=_BATCH)
    s, v = len(subjects), len(services)
    option_vecs: dict[str, dict[str, np.ndarray]] = {}
    for (qid, label), vec in zip(option_keys, vecs[s + v :], strict=True):
        option_vecs.setdefault(qid, {})[label] = vec
    return MappingVectors(tuple(subjects), tuple(services), vecs[:s], vecs[s : s + v], option_vecs)


def _team_counts(wh: duckdb.DuckDBPyConnection, vectors: MappingVectors
                 ) -> tuple[np.ndarray, np.ndarray]:  # fmt: skip
    """Per subject its team incident total (s,) and per (subject, service) count (s, v)."""
    rows_of = {s.team_id: i for i, s in enumerate(vectors.subjects) if s.subject_type == "team"}
    cols_of = {svc.service_id: j for j, svc in enumerate(vectors.services)}
    totals = np.zeros(len(vectors.subjects), dtype=np.int64)
    on = np.zeros((len(vectors.subjects), len(vectors.services)), dtype=np.int64)
    params: dict[str, object] = {"ids": list(rows_of)}
    for team_id, service_id, n in _fetch(wh, _COOC_SQL, params, "cooc") if rows_of else []:
        i = rows_of[team_id]
        totals[i] += n
        if service_id in cols_of:
            on[i, cols_of[service_id]] = n
    return totals, on


def _payload(vectors: MappingVectors, scores: ScoreMatrices, i: int, j: int,
             counts: tuple[int, int]) -> dict[str, object]:  # fmt: skip
    """One `mapping_suggestion` payload (design 03 §4.6)."""
    subject = vectors.subjects[i]
    return {
        "subject_type": subject.subject_type, "jira_project": subject.jira_project,
        "jira_component": subject.jira_component, "team_id": subject.team_id,
        "service_id": vectors.services[j].service_id,
        **{name: round(float(getattr(scores, name)[i, j]), _ROUND) for name in _SCORES},
        "evidence_counts": {"work_items": subject.work_items, "team_incidents": counts[0],
                            "team_incidents_on_service": counts[1]},
        "algorithm_version": ALGORITHM_VERSION,
    }  # fmt: skip


def _top(row: np.ndarray, service_ids: np.ndarray, min_score: float, top_n: int) -> list[int]:
    """Columns with `score >= min_score`, by score desc then service_id, at most `top_n`."""
    ok = np.flatnonzero(row >= min_score)
    order = ok[np.lexsort((service_ids[ok], -row[ok]))]
    return [int(j) for j in order[:top_n]]


def run_suggest_stage(
    wh: duckdb.DuckDBPyConnection, *, vectors: MappingVectors | None, cfg: DecisionsConfig,
    report: _Report,
) -> None:  # fmt: skip
    """Stage `suggest`: emit `pending` `mapping_suggestion` review items (U03-114).

    Skipped without vectors. No re-emit over a `pending` or `rejected` item for the same
    (subject, service_id); nothing is approved, `core.service_map` is never written (TH03-10).
    Raises SchemaViolation on a DuckDB error; ops errors (after impl 02 retries) propagate.
    """
    if vectors is None:
        report.status = "skipped"
        _log.info("enrich.suggest.skipped", reason="no_vectors")
        return
    ms = cfg.mapping_suggest
    totals, on = _team_counts(wh, vectors)
    cooc = np.divide(on, totals[:, None], out=np.zeros(on.shape), where=totals[:, None] > 0)
    subjects, services = vectors.subjects, vectors.services
    scores = mapping_scores(
        subject_names=[s.name for s in subjects], subject_types=[s.subject_type for s in subjects],
        service_names=[svc.name for svc in services], subject_vecs=vectors.subject_vecs,
        service_vecs=vectors.service_vecs, cooccurrence=cooc, weights=ms.weights,
        abbreviations=ms.abbreviations,
    )  # fmt: skip
    service_ids = np.asarray([svc.service_id for svc in services], dtype=str)
    payloads = [
        _payload(vectors, scores, i, j, (int(totals[i]), int(on[i, j])))
        for i in range(len(subjects))
        for j in _top(scores.score[i], service_ids, ms.min_score, ms.top_n)
    ]
    created, suppressed = 0, 0
    if payloads:
        created, suppressed = create_if_absent(
            "mapping_suggestion", payloads, match_keys=_MATCH_KEYS,
            blocking_statuses=("pending", "rejected"), now=clock.now(),
        )  # fmt: skip
    record_counter("herness_enrich_mapping_suggestions_total", created, component="enrich")
    report.rows += created
    _log.info("enrich.suggest.emitted", created=created, suppressed=suppressed,
              subjects=len(subjects), services=len(services))  # fmt: skip
