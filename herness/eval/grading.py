"""Grading methods and the unsupported-number count (design 11 §5.3.2; U11-56 … U11-60).

Numeric, entity, rule and rubric grading of one pipeline output against a resolved golden
question, plus `count_unsupported`, which is independent of the Verifier (TH11-12). Marker
parsing and numeral scanning are the single implementation in `herness.core.numbers`
(R-16); the claimed-vs-rerun cell comparison and row selection are spec 05 §5.6 steps 6-7
from `herness.harness.verifier`. Details hold ids, values and suite text, never answer prose.
"""

from __future__ import annotations

import math
import re
import statistics
from collections.abc import Callable, Iterable, Mapping, Sequence
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal

from pydantic import BaseModel, ConfigDict
from scipy.stats import kendalltau  # type: ignore[import-untyped]  # scipy ships no types

from herness.core.errors import EgressBlocked, ModelUnavailable
from herness.core.numbers import find_uncited, parse_markers
from herness.core.types import ChatAnswer, NumberRef, Paragraph, RecommendationItem
from herness.eval.golden import (
    ClaimRule,
    EntitiesExpected,
    MentionRule,
    NumericExpected,
    ReferenceResult,
    RubricExpected,
    RulesExpected,
    SignRule,
    SuiteError,
)
from herness.eval.judge import RubricJudge
from herness.harness.verifier import compare_value, row_matches

__all__ = [
    "EvidenceLookup", "GradeResult", "NumberCarrier", "RerunFn",
    "UnsupportedReport", "carriers_from", "chat_entity_ids", "count_unsupported",
    "grade_entities", "grade_numeric", "grade_rubric", "grade_rules", "kendall_tau_check",
    "split_sentences",
]  # fmt: skip

type GradeStatus = Literal["passed", "failed", "skipped"]
type Row = Mapping[str, object]
type EvidenceLookup = Callable[[str, str], bool]
"""`(query_id, build_id)` → True when ops `evidence`, else warehouse `meta.evidence`, holds
the query for that build (DD11-09 default: ops then `meta.evidence`)."""
type RerunFn = Callable[[str], Sequence[Row]]
"""`query_id` → the full result of the recorded SQL re-run read-only, rows as column → cell;
raising any exception means the rerun failed."""

_ZERO_TOL: Final = Decimal("1e-9")
_TAU_EPS: Final = 1e-9  # scipy's tau carries float error (1.0 → 0.9999999999999999)
_SENTENCE_RE: Final = re.compile(r"(?<=[.!?])\s+|\n")
_MASK: Final = "\x00"  # a non-word character, so masking never creates a new word boundary
_INTEGER_PY_TYPE: Final = "BIGINT"


class GradeResult(BaseModel):
    """One graded check; `detail` is JSON-safe and carries no answer prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    check: str
    status: GradeStatus
    detail: dict[str, Any]


class NumberCarrier(BaseModel):
    """Text with `[[nK]]` markers and the NumberRefs it may cite (U11-56)."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    text: str
    numbers: list[NumberRef]


class UnsupportedReport(BaseModel):
    """Result of `count_unsupported` (U11-60); lists hold numerals, marker ids and ref ids."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    total: int
    unsupported: int
    stray_numerals: list[str]
    orphan_markers: list[str]
    stale_refs: list[str]
    rate: float


def _result(check: str, ok: bool, **detail: Any) -> GradeResult:  # noqa: ANN401 - JSON values
    return GradeResult(check=check, status="passed" if ok else "failed", detail=detail)


def carriers_from(
    items: Iterable[ChatAnswer | Paragraph | RecommendationItem],
) -> list[NumberCarrier]:
    """One carrier per chat answer, paragraph or recommendation (`headline` + `summary`)."""
    out: list[NumberCarrier] = []
    for item in items:
        text = (
            f"{item.headline}\n{item.summary}"
            if isinstance(item, RecommendationItem)
            else item.text
        )
        out.append(NumberCarrier(text=text, numbers=list(item.numbers)))
    return out


def _decimal(value: object) -> Decimal | None:
    if isinstance(value, bool) or not isinstance(value, int | float | str | Decimal):
        return None
    try:
        number = Decimal(repr(value)) if isinstance(value, float) else Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return number if number.is_finite() else None


def _reference_value(ref: ReferenceResult) -> Decimal:
    if len(ref.rows) != 1:
        msg = "numeric reference needs exactly one row"
        raise SuiteError(msg, rows=len(ref.rows))
    if "value" in ref.columns:
        cell = ref.rows[0][ref.columns.index("value")]
    elif len(ref.columns) == 1:
        cell = ref.rows[0][0]
    else:
        msg = "numeric reference needs a `value` column or exactly one column"
        raise SuiteError(msg, columns=len(ref.columns))
    number = _decimal(cell)
    if number is None:
        msg = "numeric reference value is not a finite number"
        raise SuiteError(msg, query_id=ref.query_id)
    return number


def _within(value: Decimal, reference: Decimal, exp: NumericExpected) -> bool:
    tol = exp.tolerance
    diff = abs(value - reference)
    if tol is not None and tol.rel is not None:
        if reference == 0:
            return abs(value) <= _ZERO_TOL
        return diff <= Decimal(repr(tol.rel)) * abs(reference)
    limit = Decimal(repr(tol.abs)) if tol is not None and tol.abs is not None else Decimal(0)
    return diff <= limit


def grade_numeric(
    exp: NumericExpected, ref: ReferenceResult, carriers: Sequence[NumberCarrier]
) -> GradeResult:
    """Pass when a cited NumberRef of the expected unit is within tolerance (U11-56)."""
    reference = _reference_value(ref)
    candidates: list[dict[str, str]] = []
    matched = False
    for carrier in carriers:
        cited = set(parse_markers(carrier.text).ids)
        for number in carrier.numbers:
            if number.id not in cited or number.unit != exp.unit:
                continue
            value = _decimal(number.value)
            candidates.append({"id": number.id, "value": str(number.value)})
            matched = matched or (value is not None and _within(value, reference, exp))
    return _result(
        "numeric", matched, reference=str(reference), query_id=ref.query_id, candidates=candidates
    )


def kendall_tau_check(
    ranked: Sequence[str], reference: Sequence[str], threshold: float
) -> tuple[bool, float]:
    """Tau-b of the reference items' positions in `ranked` (missing → `len(ranked)`)."""
    if len(reference) < 2:  # noqa: PLR2004 - tau needs two items
        msg = "kendall_tau needs at least two reference items"
        raise SuiteError(msg, n=len(reference))
    index = {item: i for i, item in reversed(list(enumerate(ranked)))}
    positions = [index.get(item, len(ranked)) for item in reference]
    tau = float(kendalltau(positions, list(range(len(reference))), variant="b").statistic)
    return (not math.isnan(tau) and tau >= threshold - _TAU_EPS), tau


def grade_entities(
    exp: EntitiesExpected, ref: ReferenceResult, ranked_ids: Sequence[str]
) -> GradeResult:
    """Compare ranked entity ids with column 1 of the reference rows (U11-57)."""
    reference = [str(row[0]) for row in ref.rows]
    if not reference:
        msg = "entity reference returned no rows"
        raise SuiteError(msg, query_id=ref.query_id)
    ranked, check = list(ranked_ids), exp.check
    detail: dict[str, Any] = {"query_id": ref.query_id, "reference": reference[:20]}
    if check == "rank1":
        ok = bool(ranked) and ranked[0] == reference[0]
    elif check == "set_equals":
        ok = set(ranked[: len(reference)]) == set(reference)
    elif check.startswith("topk_contains:"):
        k, m = (int(part) for part in check.split(":")[1:])
        ok = len(set(ranked[:k]) & set(reference[:k])) >= m
    else:  # kendall_tau>=X (the loader's pattern admits nothing else)
        ok, tau = kendall_tau_check(ranked, reference, float(check.partition(">=")[2]))
        detail["tau"] = None if math.isnan(tau) else tau
    return _result(f"entities:{check}", ok, ranked=ranked[:20], **detail)


def chat_entity_ids(text: str, name_index: Mapping[str, str]) -> list[str]:
    """Entity ids named in `text`: whole words, case-insensitive, longest names first (U11-57)."""
    masked = text
    hits: list[tuple[int, str]] = []
    for name in sorted((n for n in name_index if n), key=len, reverse=True):
        pattern = re.compile(rf"(?<!\w){re.escape(name)}(?!\w)", re.IGNORECASE)
        hits.extend((match.start(), name_index[name]) for match in pattern.finditer(masked))
        masked = pattern.sub(lambda m: _MASK * len(m.group()), masked)
    return list(dict.fromkeys(entity for _, entity in sorted(hits)))


def split_sentences(text: str) -> list[str]:
    """Split after `.`, `!` or `?` plus whitespace and on newlines; strip; drop empty."""
    return [part.strip() for part in _SENTENCE_RE.split(text) if part.strip()]


def _mention(item: str | MentionRule, final_text: str, sentences: Sequence[str]) -> GradeResult:
    if isinstance(item, str):
        return _result("must_mention", item.casefold() in final_text.casefold(), item=item)
    entity = item.entity.casefold()
    phrases = [phrase.casefold() for phrase in item.with_any]
    ok = any(
        entity in low and any(p in low for p in phrases)
        for low in (sentence.casefold() for sentence in sentences)
    )
    return _result("must_mention", ok, item=item.entity, with_any=item.with_any)


def _claim(rule: ClaimRule, sources: Sequence[tuple[str, list[str]]]) -> GradeResult:
    entity, regex = rule.entity.casefold(), rule.regex
    where = next(
        (
            name
            for name, sentences in sources
            for sentence in sentences
            if entity in sentence.casefold() and regex.search(sentence)
        ),
        None,
    )
    return _result("must_not_claim", where is None, entity=rule.entity, where=where)


def _sign(rule: SignRule, numbers: Sequence[NumberRef]) -> GradeResult:
    values = [_decimal(n.value) for n in numbers if n.column == rule.column]
    if rule.sign == "positive":
        ok = any(v is not None and v > 0 for v in values)
    else:
        ok = any(v is not None and v < 0 for v in values)
    return _result("number_signs", ok, column=rule.column, sign=rule.sign)


def grade_rules(
    rules: RulesExpected,
    final_text: str,
    verified_claims: Sequence[str],
    numbers: Sequence[NumberRef],
) -> list[GradeResult]:
    """One GradeResult per rule item, in the order mention, claim, count, sign (U11-58)."""
    sentences = split_sentences(final_text)
    out = [_mention(item, final_text, sentences) for item in rules.must_mention]
    sources = [("final_text", sentences)]
    sources += [(f"claim[{i}]", split_sentences(c)) for i, c in enumerate(verified_claims)]
    out += [_claim(rule, sources) for rule in rules.must_not_claim]
    if rules.max_number_refs is not None:
        count = len(numbers)
        ok = count <= rules.max_number_refs
        out.append(_result("max_number_refs", ok, count=count, max=rules.max_number_refs))
    out += [_sign(rule, numbers) for rule in rules.number_signs]
    return out


def grade_rubric(
    exp: RubricExpected, question_id: str, final_text: str, judge: RubricJudge | None
) -> GradeResult:
    """Judge prose against the rubric; skipped without a judge, when down or blocked (U11-59)."""
    if judge is None:
        return GradeResult(check="rubric", status="skipped", detail={"reason": "no_judge"})
    try:
        scored = judge.score(question_id, exp.criteria, exp.min_score, final_text)
    except ModelUnavailable:
        return GradeResult(check="rubric", status="skipped", detail={"reason": "judge_unavailable"})
    except EgressBlocked:  # hosted judge refused by the egress guard (TH11-06)
        detail = {"reason": "judge_egress_blocked"}
        return GradeResult(check="rubric", status="skipped", detail=detail)
    scores = dict(scored.scores)
    mean = statistics.fmean(scores.values()) if scores else None
    ok = mean is not None and mean >= exp.min_score
    return _result("rubric", ok, mean=mean, min_score=exp.min_score, scores=scores)


def _py_type(cell: object) -> str:
    """DuckDB type family of a fetched cell for `compare_value` (DECIMAL, integer, DOUBLE)."""
    if isinstance(cell, Decimal):
        return "DECIMAL"
    return _INTEGER_PY_TYPE if isinstance(cell, int) and not isinstance(cell, bool) else "DOUBLE"


class _Checker:
    """Supports NumberRefs against evidence and one cached rerun per query id."""

    def __init__(
        self, build_id: str, evidence: EvidenceLookup, rerun: RerunFn, rel_tol: float
    ) -> None:
        self._build_id, self._evidence, self._rerun = build_id, evidence, rerun
        self._rel_tol = rel_tol
        self._rows: dict[str, Sequence[Row] | None] = {}

    def _result_rows(self, query_id: str) -> Sequence[Row] | None:
        if query_id not in self._rows:
            rows: Sequence[Row] | None = None
            if self._evidence(query_id, self._build_id):
                try:
                    rows = self._rerun(query_id)
                except Exception:  # noqa: BLE001 - any rerun failure makes the ref unsupported
                    rows = None
            self._rows[query_id] = rows
        return self._rows[query_id]

    def supported(self, number: NumberRef) -> bool:
        rows = self._result_rows(number.query_id)
        if rows is None:
            return False
        key = number.row_key or {}
        selected = [row for row in rows if row_matches(row, key)]
        if len(selected) != 1 or number.column not in selected[0]:
            return False
        cell = selected[0][number.column]
        return compare_value(
            number.value, cell, unit=number.unit, duckdb_type=_py_type(cell), rel_tol=self._rel_tol
        )


def count_unsupported(  # noqa: PLR0913 - signature fixed by spec 11 U11-60
    text: str,
    numbers: Sequence[NumberRef],
    build_id: str,
    *,
    evidence: EvidenceLookup,
    rerun: RerunFn,
    allowed_patterns: Sequence[re.Pattern[str]],
    float_rel_tol: float,
) -> UnsupportedReport:
    """Count stray numerals, stale NumberRefs and orphan markers (U11-60, TH11-12)."""
    markers = parse_markers(text).ids
    stray = [hit.text for hit in find_uncited(text, allowed_patterns)]
    by_id: dict[str, NumberRef] = {}
    for number in numbers:
        by_id.setdefault(number.id, number)
    checker = _Checker(build_id, evidence, rerun, float_rel_tol)
    stale = [nid for nid, number in by_id.items() if not checker.supported(number)]
    orphans = [marker for marker in markers if marker not in by_id]
    total = len(markers) + len(stray)
    unsupported = len(stray) + len(stale) + len(orphans)
    return UnsupportedReport(
        total=total,
        unsupported=unsupported,
        stray_numerals=stray,
        orphan_markers=orphans,
        stale_refs=stale,
        rate=unsupported / total if total else 0.0,
    )
