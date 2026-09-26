"""Deterministic number verification (impl 05 §3.7, R-37: numbers only, no claim checker).

``compare_value``, ``canonical_cell_text`` and ``row_matches`` are steps 6 and 7 of design
§5.6: select the cited row and compare the claimed value with the re-run result. Pure: no
I/O, clock, logging or configuration access. The numeral scanner and marker parser are
impl 00's (R-16, ``herness.core.numbers``); this module does not reimplement them.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, date, datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from typing import Final

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
