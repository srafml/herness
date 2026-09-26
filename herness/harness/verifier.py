"""Deterministic number verification (impl 05 §3.7, R-37: numbers only, no claim checker).

``compare_value``, ``canonical_cell_text`` and ``row_matches`` are steps 6 and 7 of design
§5.6: select the cited row and compare the claimed value with the re-run result; they are pure.
``Verifier`` runs design §5.6 steps 1-10 (step 9 removed by R-37) over ``VerifiableItem``s:
markers, uncited numerals, named refs and finding statuses, then one cached re-run per cited
query with the cell tolerance of design 04 §4.4 (R-15). No model is called. The numeral
scanner and marker parser are impl 00's (R-16, ``herness.core.numbers``); the re-run
machinery and the item-level helpers live in the private siblings ``_verifier_rerun`` and
``_verifier_items`` (module budget).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError
from herness.core.logging import get_logger
from herness.core.numbers import compile_allowed_patterns, find_uncited
from herness.core.types import (
    ChatAnswer,
    Finding,
    ItemResult,
    NumberCheck,
    NumberRef,
    OpsHandle,
    ReportDraft,
    TraceEmitter,
    UncitedSpan,
    VerifiableItem,
    VerificationResult,
)
from herness.harness import _verifier_items as it
from herness.harness import _verifier_rerun as rr
from herness.harness.llm.settings import SqlSettings, VerifierSettings
from herness.harness.warehouse import BUILD_ID_RE, WarehousePool
from herness.store.ops import _shims

_INTEGER_DUCKDB_TYPES: Final = frozenset(
    {
        "TINYINT",
        "SMALLINT",
        "INTEGER",
        "BIGINT",
        "HUGEINT",
        "UTINYINT",
        "USMALLINT",
        "UINTEGER",
        "UBIGINT",
        "UHUGEINT",
    }
)
_COUNT_LIKE_UNITS: Final = frozenset({"count", "rank"})
_TINY: Final = Decimal("1e-9")
_SECONDS_FMT: Final = "%Y-%m-%dT%H:%M:%SZ"
_PRECISION: Final = 60


def _claim_decimal(claimed: float | int | str) -> Decimal:
    return Decimal(repr(claimed)) if isinstance(claimed, float) else Decimal(claimed)


def _quantized(value: Decimal, places: int) -> Decimal:
    return value.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_EVEN)


def _exponent_places(value: Decimal) -> int:
    return max(0, -value.as_tuple().exponent)  # type: ignore[operator]


def _decimal_or_close(
    claimed: float | int | str, actual: object, *, unit: str, duckdb_type: str, rel_tol: float
) -> bool:
    if unit == "usd" or duckdb_type.startswith("DECIMAL"):
        claim = _claim_decimal(claimed)
        actual_decimal = Decimal(str(actual))
        return _quantized(actual_decimal, _exponent_places(claim)) == claim
    if duckdb_type in _INTEGER_DUCKDB_TYPES or unit in _COUNT_LIKE_UNITS:
        return _claim_decimal(claimed) == Decimal(str(actual))
    claim = _claim_decimal(claimed)
    actual_decimal = Decimal(repr(float(actual)))  # type: ignore[arg-type]
    if _quantized(actual_decimal, _exponent_places(claim)) == claim:
        return True
    tolerance = Decimal(str(rel_tol)) * abs(actual_decimal)
    if abs(claim - actual_decimal) <= tolerance:
        return True
    return abs(claim) < _TINY and abs(actual_decimal) < _TINY


def compare_value(
    claimed: float | int | str,
    actual: object,
    *,
    unit: str,
    duckdb_type: str,
    rel_tol: float,
) -> bool:
    """Steps 6-7 of design §5.6: does ``claimed`` match the re-run cell ``actual``.

    Deterministic and pure; invalid numeric text or a non-numeric ``actual`` compares
    as ``False`` rather than raising (unit spec U05-66).
    """
    if actual is None or isinstance(actual, bool) or not isinstance(actual, int | float | Decimal):
        return False
    try:
        with localcontext() as ctx:
            ctx.prec = _PRECISION
            return _decimal_or_close(
                claimed, actual, unit=unit, duckdb_type=duckdb_type, rel_tol=rel_tol
            )
    except (ArithmeticError, ValueError, TypeError):
        return False


def _datetime_seconds_text(value: datetime) -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime(_SECONDS_FMT)


def canonical_cell_text(value: object) -> str | int | float | bool | Decimal | None:
    """Render one DuckDB cell as the text row_matches and reports compare against."""
    if value is None or isinstance(value, bool | int | float | Decimal):
        return value
    if isinstance(value, datetime):
        return _datetime_seconds_text(value)
    if isinstance(value, date):
        return value.isoformat()
    return str(value)


def _numeric_key_matches(key: int | float, cell: object) -> bool:
    if isinstance(cell, bool):
        return False
    if isinstance(cell, int | float | Decimal):
        try:
            return Decimal(str(key)) == Decimal(str(cell))
        except ArithmeticError:
            return False
    if isinstance(cell, str):
        return str(key) == cell
    return key == canonical_cell_text(cell)


def _string_key_matches(key: str, cell: object) -> bool:
    if not isinstance(cell, bool) and isinstance(cell, int | float | Decimal):
        try:
            return Decimal(key) == Decimal(str(cell))
        except ArithmeticError:
            return False
    if isinstance(cell, datetime):
        if key == _datetime_seconds_text(cell):
            return True
        return key == cell.isoformat().replace("+00:00", "Z")
    return key == canonical_cell_text(cell)


def _key_matches(key: str | int | float | bool | None, cell: object) -> bool:
    if key is None:
        return cell is None
    if isinstance(key, bool):
        return isinstance(cell, bool) and cell == key
    if isinstance(key, int | float):
        return _numeric_key_matches(key, cell)
    return _string_key_matches(key, cell)


def row_matches(
    row: Mapping[str, object], row_key: Mapping[str, str | int | float | bool | None]
) -> bool:
    """Does ``row`` hold every ``row_key`` column with an equal value (unit spec U05-66)."""
    return all(key in row and _key_matches(expected, row[key]) for key, expected in row_key.items())


VERIFIER_CACHE_ROWS: Final = rr.VERIFIER_CACHE_ROWS
# Mirrors config/app.yaml `reports.allowed_numeral_patterns` (owner 09) for as long as the root
# config types `app` with a stub; `_configured_patterns` prefers the loaded owner section.
_FALLBACK_NUMERAL_PATTERNS: Final = (
    r"\b(19|20)\d{2}\b",
    r"\d{4}-\d{2}-\d{2}",
    r"Q[1-4] \d{4}",
    r"(INC|CHG|PRB)\d+",
    r"[A-Z][A-Z0-9]+-\d+",
)
_log = get_logger("harness.verifier")


def _configured_patterns() -> Sequence[str]:
    reports = getattr(get_config().app, "reports", None)
    patterns = getattr(reports, "allowed_numeral_patterns", None)
    return _FALLBACK_NUMERAL_PATTERNS if patterns is None else tuple(patterns)


def _ms_since(start: float) -> int:
    return max(0, round((clock.monotonic() - start) * 1000))


class Verifier:
    """Deterministic number verification over re-executed queries (design §3.5, §5.6).

    The re-run cache maps ``(query_id, build_id)`` to one re-run and is lock-protected, so
    one instance may serve several threads; see ``_verifier_rerun`` (U05-63).
    """

    def __init__(
        self,
        ops: OpsHandle,
        warehouses: WarehousePool,
        cfg: VerifierSettings,
        tracer: TraceEmitter | None = None,
        *,
        allowed_numeral_patterns: Sequence[str] | None = None,
        sql: SqlSettings | None = None,
    ) -> None:
        patterns = allowed_numeral_patterns
        self._allowed = compile_allowed_patterns(
            list(_configured_patterns() if patterns is None else patterns)
        )
        self._ops = ops
        self._warehouses = warehouses
        self._cfg = cfg
        self._tracer = tracer
        sql_settings = get_config().models.harness.sql if sql is None else sql
        self._cache = rr.RerunCache(warehouses, cfg, sql_settings, row_matches)

    def verify_numbers(self, item: VerifiableItem, build_id: str) -> VerificationResult:
        """Verify one item (U05-64); raises ``ConfigError`` for an invalid ``build_id``."""
        return self._verify_items([item], build_id)

    def verify_findings(
        self, findings: Sequence[Finding], build_id: str
    ) -> list[VerificationResult]:
        """One result per finding, in input order; the re-run cache is shared (U05-67)."""
        return [
            self._verify_items(
                [VerifiableItem(where=f"finding:{f.finding_id}", text=f.claim, numbers=f.numbers)],
                build_id,
            )
            for f in findings
        ]

    def verify_draft(self, draft: ReportDraft, build_id: str) -> VerificationResult:
        """One result with one item per text-carrying object of the draft (U05-67)."""
        return self._verify_items(it.draft_items(draft), build_id)

    def verify_answer(self, answer: ChatAnswer, build_id: str) -> VerificationResult:
        """One item ``where="answer"`` for a chat answer (U05-67)."""
        item = VerifiableItem(where="answer", text=answer.text, numbers=answer.numbers)
        return self._verify_items([item], build_id)

    def _verify_items(self, items: Sequence[VerifiableItem], build_id: str) -> VerificationResult:
        if BUILD_ID_RE.fullmatch(build_id) is None:
            msg = "invalid build_id"
            raise ConfigError(msg)
        start = clock.monotonic()
        results: list[ItemResult] = []
        for index, item in enumerate(items):
            if index:
                _shims.fault_point("verifier.mid_batch")  # T08-08: herness.core.resilience
            results.append(self._verify_item(item, build_id))
        checks = [check for result in results for check in result.checks]
        return VerificationResult(
            build_id=build_id,
            passed=all(result.passed for result in results),
            items=results,
            n_numbers=len(checks),
            n_failed=sum(1 for check in checks if check.result != "match"),
            verified_at=clock.now(),
            duration_ms=_ms_since(start),
        )

    def _verify_item(self, item: VerifiableItem, build_id: str) -> ItemResult:
        """Steps 1-8 for one item; the step 10 verdict is emitted by ``_report``."""
        start = clock.monotonic()
        unknown, bad_refs = it.marker_problems(item)
        uncited = [
            UncitedSpan(text=hit.text, start=hit.start, end=hit.end)
            for hit in find_uncited(item.text, self._allowed)
        ]
        unverified: list[str] = []
        if item.finding_ids:
            statuses = self._ops.finding_statuses(item.finding_ids)
            unverified = [
                f for f in dict.fromkeys(item.finding_ids) if statuses.get(f) != "verified"
            ]
        counts = it.HashCounts()
        checks = self._check_numbers(item.numbers, build_id, counts)
        clean = not (uncited or unknown or bad_refs or unverified)
        result = ItemResult(
            where=item.where,
            passed=clean and all(check.result == "match" for check in checks),
            checks=checks,
            uncited=uncited,
            unknown_markers=unknown,
            bad_refs=bad_refs,
            unverified_findings=unverified,
        )
        self._report(result, build_id, counts, _ms_since(start))
        return result

    def _check_numbers(
        self, numbers: Sequence[NumberRef], build_id: str, counts: it.HashCounts
    ) -> list[NumberCheck]:
        """Steps 5-7, one group per ``query_id``; checks keep the order of ``numbers``."""
        checks: dict[int, NumberCheck] = {}
        for query_id, indexes in it.group_by_query(numbers).items():
            group = self._check_group(query_id, [numbers[i] for i in indexes], build_id, counts)
            checks.update(zip(indexes, group, strict=True))
        return [checks[i] for i in range(len(numbers))]

    def _check_group(
        self, query_id: str, refs: Sequence[NumberRef], build_id: str, counts: it.HashCounts
    ) -> list[NumberCheck]:
        """Steps 5a-5e for one cited query: evidence, build check, re-run, cell tolerance."""
        ev = self._ops.get_evidence(query_id)
        if ev is not None and ev.build_id != build_id:
            return [it.make_check(ref, "wrong_build") for ref in refs]
        if ev is not None:
            stored: rr.StoredEvidence | None = rr.from_ops(ev)
        else:
            stored = rr.load_meta(self._warehouses, query_id, build_id)
        if stored is None:
            return [it.make_check(ref, "missing_query") for ref in refs]
        keys = {rr.key_token(ref.row_key): ref.row_key for ref in refs}
        rerun = self._cache.get(query_id, stored, build_id, keys)  # U05-63 `_rerun`
        kind = None if rerun.error is not None else rr.hash_kind(stored, rerun)
        if kind is None:  # a re-run error, or `result drift`
            return [it.make_check(ref, "query_failed") for ref in refs]
        setattr(counts, kind, getattr(counts, kind) + 1)
        return [self._check_ref(ref, rerun) for ref in refs]

    def _check_ref(self, ref: NumberRef, rerun: rr.Rerun) -> NumberCheck:
        """Steps 6-7: column, row selection by ``row_key``, value comparison."""
        if ref.column not in rerun.columns:
            return it.make_check(ref, "missing_column")
        match = rerun.matches[rr.key_token(ref.row_key)]
        if match.first is None:
            return it.make_check(ref, "row_not_found")
        if match.count > 1:
            return it.make_check(ref, "row_ambiguous")
        column = rerun.columns.index(ref.column)
        actual = match.first[column]
        ok = compare_value(
            ref.value,
            actual,
            unit=ref.unit,
            duckdb_type=rerun.types[column],
            rel_tol=self._cfg.float_rel_tol,
        )
        return it.make_check(ref, "match" if ok else "mismatch", actual)

    def _report(
        self, result: ItemResult, build_id: str, counts: it.HashCounts, duration_ms: int
    ) -> None:
        """Step 10: the `verifier_verdict` trace, metrics and the log line of a failed item."""
        n_failed = sum(1 for check in result.checks if check.result != "match")
        if self._tracer is not None:
            self._tracer.emit(
                "verifier_verdict",
                where=result.where,
                passed=result.passed,
                n_numbers=len(result.checks),
                n_failed=n_failed,
                n_uncited=len(result.uncited),
                duration_ms=duration_ms,
                n_hash_equal=counts.equal,
                n_hash_equivalent=counts.equivalent,
                n_hash_values_only=counts.values_only,
            )
        # T08-05: herness_harness_verifier_items_total{passed} += 1 per item
        # T08-05: herness_harness_verifier_checks_total{result} += 1 per check
        if not result.passed:
            _log.info(
                "harness.verifier.item_failed",
                where=result.where,
                n_failed=n_failed,
                n_uncited=len(result.uncited),
                build_id=build_id,
            )
