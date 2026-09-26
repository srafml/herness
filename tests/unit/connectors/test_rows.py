"""Tests for herness.connectors.rows name, flattening and timestamp helpers (U01-22 to U01-24)."""

import datetime
import decimal
import json
import re

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.connectors import rows
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
_SNAKE = re.compile(r"[a-z][a-z0-9_]{0,127}")
HOUR_2 = datetime.timedelta(hours=2)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("sys_id", "sys_id"),
        ("_id", "f_id"),
        ("HTTPStatus", "http_status"),
        ("1x", "f_1x"),
        ("CC_ID", "cc_id"),
        ("a.b-c", "a_b_c"),
        ("createdAt", "created_at"),
        ("fooBARBaz", "foo_bar_baz"),
        ("__a__b__", "f_a_b"),
        ("näme", "n_me"),
        ("a" * 128, "a" * 128),
    ],
)
def test_ut01_15_to_snake_table(name: str, expected: str) -> None:
    """UT01-15 source field names map to the expected lake column names."""
    assert rows.to_snake(name) == expected


@pytest.mark.parametrize("name", ["___", "", "..", "é", "a" * 129, "_" + "a" * 127, "a" * 257])
def test_ut01_15_to_snake_unusable(name: str) -> None:
    """UT01-15 names that are empty after cleaning, too long or not text raise."""
    with pytest.raises(SchemaViolation, match="unusable field name"):
        rows.to_snake(name)


@given(st.text(max_size=300))
def test_pt01_01_to_snake_pattern_and_idempotent(name: str) -> None:
    """PT01-01 output matches the column pattern or raises; to_snake is idempotent."""
    try:
        out = rows.to_snake(name)
    except SchemaViolation:
        return
    assert _SNAKE.fullmatch(out) is not None
    assert rows.to_snake(out) == out


def test_ut01_16_flatten_record_values() -> None:
    """UT01-16 scalars, bool, float, dict, list, datetimes and display pairs become strings."""
    record = {
        "sysId": "abc",
        "count": 3,
        "active": True,
        "off": False,
        "ratio": 0.5,
        "amount": decimal.Decimal("1.10"),
        "meta": {"b": 1, "a": "é"},
        "tags": ["x", 2],
        "state": {"value": "1", "display_value": "New"},
        "odd": {"value": "1", "display_value": "New", "link": "u"},
        "at": datetime.datetime(2024, 1, 2, 3, 4, 5, tzinfo=datetime.timezone(HOUR_2)),
        "naive": datetime.datetime(2024, 1, 2),  # noqa: DTZ001 - naive on purpose
        "none": None,
    }
    out = rows.flatten_record(record, display_pairs=True)
    assert list(out) == [
        "sys_id", "count", "active", "off", "ratio", "amount", "meta", "tags",
        "state", "state_display", "odd", "at", "naive", "none",
    ]  # fmt: skip
    assert out["count"] == "3"
    assert (out["active"], out["off"]) == ("true", "false")
    assert out["ratio"] == "0.5"
    assert out["amount"] == "1.10"
    assert out["meta"] == '{"b":1,"a":"é"}'
    assert out["tags"] == '["x",2]'
    assert (out["state"], out["state_display"]) == ("1", "New")
    assert json.loads(out["odd"] or "") == record["odd"]
    assert out["at"] == "2024-01-02T01:04:05Z"
    assert out["naive"] == "2024-01-02 00:00:00"
    assert out["none"] is None


def test_ut01_16_flatten_record_fields_and_pairs_off() -> None:
    """UT01-16 ``fields`` selects and orders keys; missing keys give None; pairs off keeps JSON."""
    record = {"b": 1, "a": {"value": "1", "display_value": "x"}}
    assert rows.flatten_record(record, fields=["a", "zz", "b"]) == {
        "a": '{"value":"1","display_value":"x"}',
        "zz": None,
        "b": "1",
    }


@pytest.mark.parametrize(
    ("record", "kwargs"),
    [
        ({"sysId": 1, "sys_id": 2}, {}),
        ({"state": {"value": 1, "display_value": 2}, "stateDisplay": 3}, {"display_pairs": True}),
        ({"a": 1}, {"fields": ["a", "a"]}),
    ],
)
def test_ut01_16_flatten_record_collision(
    record: dict[str, object], kwargs: dict[str, object]
) -> None:
    """UT01-16 two keys producing one column name raise SchemaViolation."""
    with pytest.raises(SchemaViolation, match="column collision"):
        rows.flatten_record(record, **kwargs)  # type: ignore[arg-type]


_JSON = st.recursive(
    st.none() | st.booleans() | st.integers() | st.floats(allow_nan=False) | st.text(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3),
    max_leaves=10,
)


@given(st.dictionaries(st.from_regex(r"[a-z][a-z0-9]{0,10}", fullmatch=True), _JSON, max_size=8))
def test_pt01_04_flatten_values_are_text(record: dict[str, object]) -> None:
    """PT01-04 values are str or None; dict and list values round-trip through json.loads."""
    out = rows.flatten_record(record)
    for key, value in record.items():
        text = out[key]
        assert text is None or isinstance(text, str)
        if isinstance(value, dict | list):
            assert json.loads(text or "") == value
