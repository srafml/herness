"""Cluster descriptors, c-TF-IDF terms and naming (impl 03 U03-98 ... U03-102; design 03
§5.3 steps 7-8).

Descriptors are one grouped SQL query over the members view joined to `core.incident`.
Top terms come from one TF-IDF document per cluster with redaction placeholders removed
first, so pseudonyms never become terms or labels (TH03-03). Naming sends the largest
candidates to the `cluster_namer` model, each example in its own R-20 `<untrusted_data>`
block (TH03-01), under `aretry_call("llm_local", breaker_key="decider:llm")`; any failure
gives an auto label. Labels are stripped of control characters and cut to 60 characters
even when a client ignores the schema. Logs carry cluster ids and error classes only.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Final, Literal

import duckdb
import numpy as np
from pydantic import JsonValue
from sklearn.feature_extraction.text import (  # type: ignore[import-untyped]  # sklearn: no types
    TfidfVectorizer,
)

from herness.core.errors import (
    ConfigError,
    HernessError,
    ModelUnavailable,
    OutputValidationError,
    SchemaViolation,
)
from herness.core.logging import get_logger
from herness.core.resilience import aretry_call, complete_validated
from herness.core.types import LLMRequest, LLMResponse, Message, RequestMeta, SystemBlock, TextPart
from herness.enrich.deciders.llm import CompletionClient, wrap_untrusted

if TYPE_CHECKING:
    import pyarrow as pa

__all__ = [
    "NameResult",
    "NamingCandidate",
    "describe_clusters",
    "name_clusters",
    "needs_naming",
    "representative_texts",
    "top_terms_ctfidf",
]

_VIEW_RE: Final = re.compile(r"^[a-z_]{1,32}$")
_SCHEMA_ERRORS: Final = (duckdb.CatalogException, duckdb.BinderException)
_PLACEHOLDER_RE: Final = re.compile(r"\[[A-Z_]+(?:_[0-9a-f]+)?\]")
_TOP_TERMS: Final = 10
_MIN_SHARE_PCT: Final = 5  # a service is listed with >= 5 % of the members ...
_MAX_SERVICES: Final = 5  # ... and at most 5 are listed
_SIZE_RATIO: Final = (0.5, 2.0)
_MAX_LABEL: Final = 60
_AUTO_TERMS: Final = 3
_ROLE: Final = "cluster_namer"
_SCHEMA_NAME: Final = "cluster_name"
_TEMPERATURE: Final = 0.2
_MAX_REPAIRS: Final = 2
_CALL_TIMEOUT_S: Final = 120.0  # per model call; LLMRequest requires one (spec silent)
_MAX_OUTPUT_TOKENS: Final = 256  # a 60-character label and one category
_NO_RUN: Final = "run_" + "0" * 26  # the null tracer's run id, as the LLM decider uses
_HEADING_RE: Final = re.compile(r"^## (.+?)[ \t]*$", re.MULTILINE)
_NORM_EPS: Final = 1e-12

_log = get_logger("enrich.cluster")

# `{view}` is a validated identifier (`_VIEW_RE`); every value is bound or computed in SQL.
_DESCRIBE_SQL: Final = """
WITH m AS (
    SELECT v.cluster_id, i.opened_at, i.service_id
    FROM {view} AS v JOIN core.incident AS i ON i.record_id = v.record_id
),
c AS (
    SELECT cluster_id, count(*) AS size, min(opened_at) AS first_seen,
           max(opened_at) AS last_seen
    FROM m GROUP BY cluster_id
),
s AS (
    SELECT cluster_id, service_id, count(*) AS n
    FROM m WHERE service_id IS NOT NULL GROUP BY cluster_id, service_id
),
r AS (
    SELECT s.cluster_id, s.service_id,
           row_number() OVER (PARTITION BY s.cluster_id ORDER BY s.n DESC, s.service_id) AS rk
    FROM s JOIN c ON c.cluster_id = s.cluster_id
    WHERE s.n * 100 >= c.size * ?
)
SELECT c.cluster_id, c.size, c.first_seen, c.last_seen,
       coalesce(list(r.service_id ORDER BY r.rk) FILTER (WHERE r.rk <= ?), []::VARCHAR[])
           AS service_ids
FROM c LEFT JOIN r ON r.cluster_id = c.cluster_id
GROUP BY c.cluster_id, c.size, c.first_seen, c.last_seen
ORDER BY c.cluster_id
"""


@dataclasses.dataclass(frozen=True)
class NamingCandidate:
    """A cluster to name: its terms, service names and redacted example texts."""

    cluster_id: str
    size: int
    top_terms: Sequence[str]
    service_names: Sequence[str]
    examples: Sequence[str]


@dataclasses.dataclass(frozen=True)
class NameResult:
    """A cluster's label (<= 60 characters) and, from the model, its root-cause category."""

    label: str
    root_cause_category: str | None
    source: Literal["llm", "auto"]


def describe_clusters(wh: duckdb.DuckDBPyConnection, *, members_view: str) -> pa.Table:
    """`cluster_id, size, first_seen, last_seen, service_ids` per cluster (U03-98).

    `service_ids` holds up to 5 service ids with a member share >= 5 %, by share desc then
    id. Raises SchemaViolation for an invalid view name or a missing table or column.
    """
    if not _VIEW_RE.fullmatch(members_view):
        msg = "members_view is not a plain identifier"
        raise SchemaViolation(msg)
    sql = _DESCRIBE_SQL.format(view=members_view)
    try:
        return wh.execute(sql, [_MIN_SHARE_PCT, _MAX_SERVICES]).to_arrow_table()
    except duckdb.Error as exc:
        schema_error = isinstance(exc, _SCHEMA_ERRORS) and str(exc)
        reason = str(exc).splitlines()[0] if schema_error else type(exc).__name__
        msg = f"cluster describe: {reason}"
        raise SchemaViolation(msg) from exc


def top_terms_ctfidf(docs: Mapping[str, Sequence[str]]) -> dict[str, list[str]]:
    """Top 10 TF-IDF terms per cluster, highest weight first, ties by term (U03-99).

    Each cluster's texts, placeholders removed, are one document. `min_df` is 2, or 1 with
    fewer than 2 documents; an empty vocabulary gives empty term lists.
    """
    ids = sorted(docs)
    if not ids:
        return {}
    corpus = ["\n".join(_PLACEHOLDER_RE.sub(" ", text) for text in docs[cid]) for cid in ids]
    vectorizer = TfidfVectorizer(
        ngram_range=(1, 2),
        min_df=2 if len(ids) >= 2 else 1,  # noqa: PLR2004 - the spec's document bound
        max_features=200_000,
        stop_words="english",
    )
    try:
        weights = vectorizer.fit_transform(corpus).tocsr()
    except ValueError:  # no term survives min_df and the stop words
        return {cid: [] for cid in ids}
    terms = vectorizer.get_feature_names_out()
    out: dict[str, list[str]] = {}
    for row, cid in enumerate(ids):
        start, end = weights.indptr[row], weights.indptr[row + 1]
        pairs = [
            (float(w), str(terms[j]))
            for w, j in zip(weights.data[start:end], weights.indices[start:end], strict=True)
        ]
        pairs.sort(key=lambda p: (-p[0], p[1]))
        out[cid] = [term for _, term in pairs[:_TOP_TERMS]]
    return out


def needs_naming(
    centroid: np.ndarray, size: int, named: tuple[np.ndarray, int] | None, *, rename_cos: float
) -> bool:
    """True for a new cluster, a drifted centroid (`cos < rename_cos`) or a size ratio
    against the named size outside [0.5, 2] (U03-100)."""
    if named is None:
        return True
    named_centroid, named_size = named
    if named_size <= 0:
        return True
    a = np.asarray(centroid, dtype=np.float64)
    b = np.asarray(named_centroid, dtype=np.float64)
    cos = float(a @ b) / max(float(np.linalg.norm(a) * np.linalg.norm(b)), _NORM_EPS)
    ratio = size / named_size
    return cos < rename_cos or not _SIZE_RATIO[0] <= ratio <= _SIZE_RATIO[1]


def representative_texts(
    vectors: np.ndarray,
    hashes: Sequence[str],
    texts: Sequence[str],
    centroid: np.ndarray,
    *,
    n: int = 20,
    max_chars: int = 600,
) -> list[str]:
    """Up to ``n`` texts nearest the centroid, one per hash, each <= ``max_chars`` (U03-101)."""
    if not texts:
        return []
    matrix = np.asarray(vectors, dtype=np.float64).reshape(len(texts), -1)
    sims = matrix @ np.asarray(centroid, dtype=np.float64)
    seen: set[str] = set()
    out: list[str] = []
    for i in np.argsort(-sims, kind="stable"):
        if len(out) >= n:
            break
        if hashes[i] in seen:
            continue
        seen.add(hashes[i])
        out.append(texts[i][:max_chars])
    return out


def _clean_label(label: str) -> str:
    """Control characters become spaces, format characters (bidi, zero-width) go; the
    whitespace is collapsed and the result cut to 60 characters."""
    cats = ((ch, unicodedata.category(ch)) for ch in label)
    kept = "".join(" " if cat == "Cc" else ch for ch, cat in cats if cat != "Cf")
    return " ".join(kept.split())[:_MAX_LABEL].rstrip()


def _auto(candidate: NamingCandidate) -> NameResult:
    label = "auto: " + " / ".join(candidate.top_terms[:_AUTO_TERMS])
    return NameResult(label=_clean_label(label), root_cause_category=None, source="auto")


def _system_prompt(path: Path) -> str:
    """The text after the `## System` heading (up to any next heading), stripped."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"prompt file {path.name} missing"
        raise ConfigError(msg) from exc
    parts = _HEADING_RE.split(text)[1:]  # [heading, body, heading, body, ...]
    sections: dict[str, str] = dict(zip(parts[0::2], (b.strip() for b in parts[1::2]), strict=True))
    system = sections.get("System", "")
    if not system:
        msg = f"prompt file {path.name} malformed"
        raise ConfigError(msg)
    return system


def _schema(root_cause_labels: Sequence[str] | None) -> dict[str, JsonValue]:
    props: dict[str, JsonValue] = {"label": {"type": "string", "maxLength": _MAX_LABEL}}
    required: list[JsonValue] = ["label"]
    if root_cause_labels is not None:
        props["root_cause_category"] = {"enum": list(root_cause_labels)}
        required.append("root_cause_category")
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


def _request(
    client: CompletionClient, system: str, schema: dict[str, JsonValue], c: NamingCandidate, i: int
) -> LLMRequest:
    lines = [
        "Top terms: " + ", ".join(c.top_terms),
        "Services: " + (", ".join(c.service_names) or "none"),
        "",
        "Example tickets:",
        *(wrap_untrusted(text) for text in c.examples),
    ]
    meta = RequestMeta(
        run_id=_NO_RUN, task_id=None, role=_ROLE, model_role=_ROLE, step=i,
        request_key=f"{_NO_RUN}:{i}:{_ROLE}",
    )  # fmt: skip
    return LLMRequest(
        client=client.name,
        system=[SystemBlock(text=system)],
        messages=[Message(role="user", parts=[TextPart(text="\n".join(lines))])],
        response_schema=schema,
        response_schema_name=_SCHEMA_NAME,
        max_output_tokens=_MAX_OUTPUT_TOKENS,
        temperature=_TEMPERATURE,
        thinking="off",
        timeout_s=_CALL_TIMEOUT_S,
        metadata=meta,
    )


def _named(resp: LLMResponse, root_cause_labels: Sequence[str] | None) -> NameResult:
    """The validated reply as an `llm` result; checked again here (a client may ignore the
    schema): a non-empty cleaned label and a category among the allowed labels."""
    try:
        obj = resp.parsed if resp.parsed is not None else json.loads(resp.text)
    except ValueError as exc:
        msg = "cluster name reply is not JSON"
        raise OutputValidationError(msg) from exc
    raw = obj.get("label") if isinstance(obj, dict) else None
    label = _clean_label(raw) if isinstance(raw, str) else ""
    category = obj.get("root_cause_category") if isinstance(obj, dict) else None
    if root_cause_labels is None:
        category = None
    if not label or (root_cause_labels is not None and category not in root_cause_labels):
        msg = "cluster name reply invalid"
        raise OutputValidationError(msg)
    return NameResult(
        label=label, root_cause_category=None if category is None else str(category), source="llm"
    )


async def _aname(
    ordered: Sequence[NamingCandidate],
    client: CompletionClient,
    root_cause_labels: Sequence[str] | None,
    max_calls: int,
    prompt_path: Path,
) -> dict[str, NameResult]:
    system, schema = _system_prompt(prompt_path), _schema(root_cause_labels)
    results: dict[str, NameResult] = {}
    calling = True
    for i, cand in enumerate(ordered):
        if not calling or i >= max_calls:
            results[cand.cluster_id] = _auto(cand)
            continue
        try:
            resp = await aretry_call(
                "llm_local",
                complete_validated,
                client,
                _request(client, system, schema, cand, i),
                max_repairs=_MAX_REPAIRS,
                breaker_key="decider:llm",
            )
            results[cand.cluster_id] = _named(resp, root_cause_labels)
        except HernessError as err:
            error_class = type(err).__name__
            _log.warning(
                "enrich.cluster.naming_fallback",
                cluster_id=cand.cluster_id,
                error_class=error_class,
            )
            results[cand.cluster_id] = _auto(cand)
            # CircuitOpen, and errors no retry fixes (auth, egress), stop further calls.
            calling = isinstance(err, OutputValidationError | ModelUnavailable)
    return results


def name_clusters(
    candidates: Sequence[NamingCandidate],
    *,
    client: CompletionClient | None,
    root_cause_labels: Sequence[str] | None,
    max_calls: int,
    prompt_path: Path,
) -> dict[str, NameResult]:
    """A result for every candidate (U03-102): the `max_calls` largest (size desc, then
    cluster_id) are named by ``client``; the rest, and any failed call, get
    `"auto: " + " / ".join(top_terms[:3])`. After a CircuitOpen every later candidate is
    auto. Only a missing or malformed prompt file raises (ConfigError).
    """
    ordered = sorted(candidates, key=lambda c: (-c.size, c.cluster_id))
    if client is None or max_calls <= 0:
        return {c.cluster_id: _auto(c) for c in ordered}
    return asyncio.run(_aname(ordered, client, root_cause_labels, max_calls, prompt_path))
