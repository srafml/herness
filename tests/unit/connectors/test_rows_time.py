"""Tests for herness.connectors.rows timestamp parsing (U01-24)."""

import datetime
import decimal

import pyarrow as pa
import pytest

from herness.connectors import rows
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
TS = datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=UTC)
UTC_US = pa.timestamp("us", tz="UTC")
PLUS2 = datetime.timezone(datetime.timedelta(hours=2))


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        ("2024-01-02 03:04:05", None, TS),
        (" 2024-01-02 03:04:05.5 ", None, TS.replace(microsecond=500000)),
        ("2024-01-02 03:04:05.123456789", None, TS.replace(microsecond=123456)),
        ("2024-01-02T03:04:05Z", None, TS),
        ("2024-01-02T05:04:05+0200", None, TS),
        ("2024-01-01T22:04:05-0500", None, TS),
        ("2024-01-02T03:04:05.123456789Z", None, TS.replace(microsecond=123456)),
        ("2024-01-02T03:04:05.1+00:00", None, TS.replace(microsecond=100000)),
        ("2024-01-02", None, datetime.datetime(2024, 1, 2, tzinfo=UTC)),
        (TS.astimezone(PLUS2), None, TS),
        (1704164645, "s", TS),
        (1704164645.5, "s", TS.replace(microsecond=500000)),
        (1704164645123, "ms", TS.replace(microsecond=123000)),
        ("1970-01-01", None, datetime.datetime(1970, 1, 1, tzinfo=UTC)),
        ("2100-12-31 23:59:59", None, datetime.datetime(2100, 12, 31, 23, 59, 59, tzinfo=UTC)),
    ],
)  # fmt: skip
def test_ut01_17_parse_source_timestamp_values(
    value: object, unit: str | None, expected: datetime.datetime
) -> None:
    """UT01-17 SN format, ISO Z, +0000, 9-digit fraction, date-only, aware and epoch values."""
    out = rows.parse_source_timestamp(value, field="sys_updated_on", epoch_unit=unit)  # type: ignore[arg-type]
    assert out == expected
    assert out.tzinfo is UTC


@pytest.mark.parametrize(
    ("value", "unit"),
    [
        ("2024-01-02T03:04:05", None),
        (TS.replace(tzinfo=None), None),
        (1704164645, None),
        (True, "s"),
        (1704164645, "us"),
        ("1969-12-31 23:59:59", None),
        ("2101-01-01", None),
        (-1, "s"),
        (float("nan"), "s"),
        (10**20, "s"),
        ("2024-02-30", None),
        ("2024-01-02 25:00:00", None),
        ("not a date secret", None),
        ("२०२४-०१-०२", None),
        (None, None),
        (decimal.Decimal(1704164645), "s"),
        (b"2024-01-02", None),
    ],
)
def test_ut01_17_parse_source_timestamp_errors(value: object, unit: str | None) -> None:
    """UT01-17 naive, epoch without unit, 1969, out of range and junk raise; no value echoed."""
    with pytest.raises(SchemaViolation) as info:
        rows.parse_source_timestamp(value, field="updated", epoch_unit=unit)  # type: ignore[arg-type]
    assert info.value.message == "unparseable timestamp in updated"
    assert info.value.__cause__ is None


def test_ut01_17_arrow_naive_and_aware_timestamps() -> None:
    """UT01-17 NTZ is read as UTC, zoned timestamps convert, nanoseconds truncate to µs."""
    ntz = pa.array([TS.replace(tzinfo=None)], pa.timestamp("s"))
    assert rows.parse_arrow_timestamps(ntz, field="f").to_pylist() == [TS]
    zoned = pa.array([TS], pa.timestamp("ms", tz="America/New_York"))
    out = rows.parse_arrow_timestamps(zoned, field="f")
    assert out.type == UTC_US
    assert out.to_pylist() == [TS]
    ns = pa.array([1_704_164_645_123_456_789], pa.timestamp("ns", tz="UTC"))
    assert rows.parse_arrow_timestamps(ns, field="f").to_pylist() == [
        TS.replace(microsecond=123456)
    ]


def test_ut01_17_arrow_dates_and_strings() -> None:
    """UT01-17 date32 and date64 give midnight UTC; strings parse element-wise."""
    midnight = datetime.datetime(2024, 1, 2, tzinfo=UTC)
    for dtype in (pa.date32(), pa.date64()):
        arr = pa.array([datetime.date(2024, 1, 2)], dtype)
        assert rows.parse_arrow_timestamps(arr, field="f").to_pylist() == [midnight]
    text = pa.array(["2024-01-02 03:04:05", "2024-01-02"], pa.large_string())
    out = rows.parse_arrow_timestamps(text, field="f")
    assert out.type == UTC_US
    assert out.to_pylist() == [TS, midnight]


@pytest.mark.parametrize(
    ("array", "bad"),
    [
        (pa.array([TS, None], pa.timestamp("us", tz="UTC")), 1),
        (pa.array([datetime.date(2024, 1, 2), None], pa.date32()), 1),
        (pa.array(["2024-01-02", None, "junk"], pa.string()), 2),
        (pa.array([-1, 0, 1], pa.timestamp("ns")), 1),
        (pa.array([datetime.date(1969, 12, 31), datetime.date(2101, 1, 1)], pa.date32()), 2),
        (pa.array([4_133_980_800_000], pa.date64()), 1),
    ],
)
def test_ut01_17_arrow_bad_rows(array: pa.Array, bad: int) -> None:
    """UT01-17 null, unparseable and out-of-range rows raise with the count of bad rows."""
    with pytest.raises(SchemaViolation, match=rf"unparseable timestamp in f \({bad} bad rows\)"):
        rows.parse_arrow_timestamps(array, field="f")


@pytest.mark.parametrize("array", [pa.array([1, 2]), pa.array([1.5]), pa.array([True])])
def test_ut01_17_arrow_other_types(array: pa.Array) -> None:
    """UT01-17 non-temporal, non-string arrays raise SchemaViolation."""
    with pytest.raises(SchemaViolation, match=r"^unparseable timestamp in f$"):
        rows.parse_arrow_timestamps(array, field="f")
