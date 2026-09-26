"""Tests for herness.connectors.base (U01-16 to U01-21): record_id, split_range, protocols."""

import datetime
from collections.abc import Iterator

import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.connectors import base
from herness.core.errors import ConfigError, SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
T0 = datetime.datetime(2024, 1, 1, tzinfo=UTC)
HOUR = datetime.timedelta(hours=1)


def test_ut01_14_record_id_format() -> None:
    """UT01-14 record_id joins source, entity and key with colons; keys may contain colons."""
    assert base.record_id("servicenow", "incident", "abc") == "servicenow:incident:abc"
    assert base.record_id("a", "b", "x:y:z") == "a:b:x:y:z"
    assert base.record_id("s" * 32, "e" * 64, "k" * 512).count(":") == 2


@pytest.mark.parametrize(
    ("source", "entity", "key"),
    [
        ("Servicenow", "incident", "k"),
        ("1source", "incident", "k"),
        ("s" * 33, "incident", "k"),
        ("servicenow", "Incident", "k"),
        ("servicenow", "e" * 65, "k"),
        ("servicenow", "in:c", "k"),
        ("servicenow", "incident", ""),
        ("servicenow", "incident", "k" * 513),
        ("servicenow", "incident", "secret\nkey"),
        ("servicenow", "incident", "secret\x1fkey"),
        ("servFf11ce", "incident", "k"),
        ("servicenow", "incident", 7),
    ],
)
def test_ut01_14_record_id_invalid(source: str, entity: str, key: str) -> None:
    """UT01-14 invalid parts raise SchemaViolation whose message never carries the key."""
    with pytest.raises(SchemaViolation) as info:
        base.record_id(source, entity, key)
    assert info.value.message == "invalid record key"
    assert "secret" not in str(info.value)
    assert "secret" not in repr(dict(info.value.context))


_SOURCE = st.from_regex(r"[a-z][a-z0-9_]{0,31}", fullmatch=True)
_ENTITY = st.from_regex(r"[a-z][a-z0-9_]{0,63}", fullmatch=True)
_KEY = st.text(st.characters(min_codepoint=0x20), min_size=1, max_size=512)


@given(_SOURCE, _ENTITY, _KEY)
def test_pt01_05_record_id_splits_back(source: str, entity: str, key: str) -> None:
    """PT01-05 splitting on the first two colons returns the inputs."""
    assert base.record_id(source, entity, key).split(":", 2) == [source, entity, key]


def test_ut01_28_split_range_windows() -> None:
    """UT01-28 exact multiple, remainder and empty ranges; windows are half-open."""
    exact = base.split_range(T0, T0 + 3 * HOUR, HOUR)
    assert exact == [(T0 + i * HOUR, T0 + (i + 1) * HOUR) for i in range(3)]
    rem = base.split_range(T0, T0 + 150 * datetime.timedelta(minutes=1), HOUR)
    assert rem[-1] == (T0 + 2 * HOUR, T0 + datetime.timedelta(minutes=150))
    assert len(rem) == 3
    assert base.split_range(T0, T0, HOUR) == []
    assert base.split_range(T0 + HOUR, T0, HOUR) == []
    near_max = datetime.datetime.max.replace(tzinfo=UTC) - datetime.timedelta(minutes=30)
    assert base.split_range(near_max, near_max + datetime.timedelta(minutes=10), HOUR) == [
        (near_max, near_max + datetime.timedelta(minutes=10))
    ]


@pytest.mark.parametrize(
    ("start", "end", "step"),
    [
        (T0.replace(tzinfo=None), T0 + HOUR, HOUR),
        (T0, (T0 + HOUR).replace(tzinfo=None), HOUR),
        (T0, T0 + HOUR, datetime.timedelta(0)),
        (T0, T0 + HOUR, -HOUR),
        (T0, T0 + HOUR, 3600),
        (T0, T0 + datetime.timedelta(seconds=100_001), datetime.timedelta(seconds=1)),
    ],
)
def test_ut01_28_split_range_errors(
    start: datetime.datetime, end: datetime.datetime, step: datetime.timedelta
) -> None:
    """UT01-28 naive input, non-positive step and too many windows raise ConfigError."""
    with pytest.raises(ConfigError):
        base.split_range(start, end, step)


def test_ut01_28_split_range_window_limit() -> None:
    """UT01-28 exactly 100,000 windows is allowed."""
    windows = base.split_range(T0, T0 + datetime.timedelta(seconds=100_000), datetime.timedelta(1))
    assert len(windows) == 2
    assert len(base.split_range(T0, T0 + 100_000 * HOUR, HOUR)) == 100_000


@given(
    st.datetimes(
        min_value=datetime.datetime(2000, 1, 1),  # noqa: DTZ001 - hypothesis bounds are naive
        max_value=datetime.datetime(2100, 1, 1),  # noqa: DTZ001 - hypothesis bounds are naive
        timezones=st.just(UTC),
    ),
    st.timedeltas(min_value=datetime.timedelta(0), max_value=datetime.timedelta(days=30)),
    st.timedeltas(min_value=datetime.timedelta(microseconds=1), max_value=datetime.timedelta(1)),
)
def test_pt01_03_split_range_covers_exactly(
    start: datetime.datetime, span: datetime.timedelta, step: datetime.timedelta
) -> None:
    """PT01-03 windows cover [start, end) exactly, without overlap, each at most step."""
    end = start + span
    if -(-span // step) > 100_000:
        with pytest.raises(ConfigError):
            base.split_range(start, end, step)
        return
    windows = base.split_range(start, end, step)
    if span == datetime.timedelta(0):
        assert windows == []
        return
    assert windows[0][0] == start
    assert windows[-1][1] == end
    for (lo, hi), nxt in zip(windows, [*windows[1:], None], strict=True):
        assert lo < hi
        assert hi - lo <= step
        if nxt is not None:
            assert nxt[0] == hi


def test_ut01_18_metadata_constants() -> None:
    """UT01-18 metadata constants hold the spec 02 §3.1 columns in order."""
    assert base.METADATA_SCHEMA.names == list(base.METADATA_FIELDS)
    nullable = {f.name: f.nullable for f in base.METADATA_SCHEMA}
    assert [n for n, v in nullable.items() if v] == ["_payload"]
    ts = pa.timestamp("us", tz="UTC")
    assert base.METADATA_SCHEMA.field("_source_updated_at").type == ts
    assert base.METADATA_SCHEMA.field("_fetched_at").type == ts
    assert base.METADATA_SCHEMA.field("_deleted").type == pa.bool_()
    assert pa.schema([pa.field("_source_key", pa.string(), nullable=False)]) == base.KEY_SCHEMA
    assert (base.DEFAULT_BATCH_ROWS, base.DEFAULT_CHECKPOINT_ROWS) == (10000, 500000)
    assert frozenset({"files", "monitoring"}) == base.UNORDERED_SOURCES


class _Keys:
    def list_keys(self, entity: str) -> Iterator[pa.RecordBatch]:
        yield pa.RecordBatch.from_pylist([{"_source_key": entity}], schema=base.KEY_SCHEMA)


class _Tools:
    def tools(self) -> tuple[str, ...]:
        return ("splunk",)

    def stream_key(self, tool: str) -> str:
        return f"monitoring:{tool}"

    def sync_tool(
        self,
        tool: str,
        entity: str,
        since: datetime.datetime | None,
        until: datetime.datetime,
    ) -> Iterator[pa.RecordBatch]:
        yield from ()


def test_ut01_18_capability_protocols_are_runtime_checkable() -> None:
    """UT01-18 optional capabilities are detected with isinstance (U01-17, U01-18)."""
    assert isinstance(_Keys(), base.SupportsKeyListing)
    assert not isinstance(_Keys(), base.SupportsToolStreams)
    assert isinstance(_Tools(), base.SupportsToolStreams)
    assert not isinstance(object(), base.SupportsKeyListing)
    assert next(_Keys().list_keys("k")).num_rows == 1
    assert list(_Tools().sync_tool("splunk", "e", None, T0)) == []
    assert _Tools().stream_key("splunk") == "monitoring:splunk"
