"""Tests for herness.core.ids (U00-19 … U00-34)."""

import base64
import datetime
import decimal
import enum
import hashlib
import itertools
import json
import pathlib
import secrets
import threading
import time
from typing import Any

import pytest
from hypothesis import given
from hypothesis import strategies as st

from herness.core import ids
from herness.core.errors import SchemaViolation

pytestmark = pytest.mark.unit

UTC = datetime.UTC
FIXED_MS = 1_790_000_000_000
BUILD = "20260924-211403-ABCDEF"


@pytest.fixture
def fresh_ulid_state(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ids, "_STATE", ids._UlidState())


def _decode(text: str) -> int:
    value = 0
    for char in text:
        value = value * 32 + ids.CROCKFORD_ALPHABET.index(char)
    return value


def _fixed_ns(monkeypatch: pytest.MonkeyPatch, *values: int) -> None:
    queue = list(values)
    monkeypatch.setattr(time, "time_ns", lambda: queue.pop(0) if len(queue) > 1 else queue[0])


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_19_ulid_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-19 a ULID is 26 Crockford characters whose first 10 decode to the time."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    value = ids.new_ulid()
    assert ids.is_valid_ulid(value)
    assert _decode(value[:10]) == FIXED_MS


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_20_same_millisecond_increments(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-20 within one millisecond the random part increments by one."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    monkeypatch.setattr(secrets, "randbits", lambda _bits: 12345)
    first, second = ids.new_ulid(), ids.new_ulid()
    assert _decode(second) == _decode(first) + 1
    assert second > first


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ut00_21_random_overflow_moves_to_next_ms(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT00-21 random-part overflow advances the timestamp by one millisecond."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000)
    monkeypatch.setattr(secrets, "randbits", lambda _bits: 2**80 - 1)
    first, second = ids.new_ulid(), ids.new_ulid()
    assert _decode(second[:10]) == _decode(first[:10]) + 1


def test_ut00_22_threads_unique_and_ordered() -> None:
    """UT00-22 8 threads x 10,000 ULIDs are unique and increasing per thread."""
    results: list[list[str]] = [[] for _ in range(8)]

    def work(slot: list[str]) -> None:
        slot.extend(ids.new_ulid() for _ in range(10_000))

    threads = [threading.Thread(target=work, args=(slot,)) for slot in results]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    everything = [value for slot in results for value in slot]
    assert len(set(everything)) == 80_000
    for slot in results:
        assert slot == sorted(slot)
        assert len(set(slot)) == len(slot)


def test_ut00_23_prefixed_ids() -> None:
    """UT00-23 new_id uses the design 00 §5 prefix for every kind."""
    expected = {
        ids.IdKind.RUN: "run",
        ids.IdKind.TASK: "task",
        ids.IdKind.JOB: "job",
        ids.IdKind.FINDING: "fnd",
        ids.IdKind.REC: "rec",
        ids.IdKind.OUTCOME: "out",
        ids.IdKind.MEMORY: "mem",
        ids.IdKind.CLUSTER: "cl",
        ids.IdKind.ITEM: "rev",
        ids.IdKind.REQUEST: "del",
        ids.IdKind.EGRESS: "egr",
        ids.IdKind.AUDIT: "aud",
    }
    assert dict(ids.ID_PREFIXES) == expected
    for kind, prefix in expected.items():
        value = ids.new_id(kind)
        assert value.startswith(prefix + "_")
        assert ids.is_valid_id(kind, value)


def test_ut00_24_build_id() -> None:
    """UT00-24 build_id has the stamp, 22 characters and a Crockford suffix.

    RF-2: a +05:30 time just after local midnight gives the UTC (previous) date.
    """
    value = ids.new_build_id(datetime.datetime(2026, 9, 24, 21, 14, 3, tzinfo=UTC))
    assert value.startswith("20260924-211403-")
    assert len(value) == 22
    assert all(char in ids.CROCKFORD_ALPHABET for char in value[-6:])
    with pytest.raises(SchemaViolation):
        ids.new_build_id(datetime.datetime(2026, 9, 24))  # noqa: DTZ001
    tz = datetime.timezone(datetime.timedelta(hours=5, minutes=30))
    value = ids.new_build_id(datetime.datetime(2026, 9, 25, 0, 10, tzinfo=tz))
    assert value.startswith("20260924-184000-")


def test_ut00_25_is_valid_ulid() -> None:
    """UT00-25 only canonical uppercase ULID text is valid."""
    good = ids.new_ulid()
    bad: list[Any] = [
        good.lower(),
        good[:25],
        good + "0",
        "8" + good[1:],
        good[:25] + "I",
        good[:25] + "L",
        good[:25] + "O",
        good[:25] + "U",
        12,
    ]
    assert ids.is_valid_ulid(good)
    assert not any(ids.is_valid_ulid(value) for value in bad)


def test_ut00_26_is_valid_id() -> None:
    """UT00-26 kind, prefix and ULID must all match."""
    run = ids.new_id(ids.IdKind.RUN)
    assert ids.is_valid_id(ids.IdKind.RUN, run)
    assert not ids.is_valid_id(ids.IdKind.TASK, run)
    assert not ids.is_valid_id(ids.IdKind.RUN, "run_BAD")
    assert not ids.is_valid_id(ids.IdKind.RUN, 12)


def test_ut00_27_is_valid_build_id() -> None:
    """UT00-27 only real calendar instants with the exact shape are valid."""
    assert ids.is_valid_build_id(BUILD)
    for bad in ("20261340-000000-ABCDEF", "20260924_211403_ABCDEF", "20260924-211403-abcdef"):
        assert not ids.is_valid_build_id(bad)


def test_ut00_28_make_record_id() -> None:
    """UT00-28 parts are validated and joined; failures name the part."""
    assert ids.make_record_id("jira", "issue", "10001") == "jira:issue:10001"
    cases = [
        ("Source", "issue", "k"),
        ("jira", "is-sue", "k"),
        ("jira", "issue", "a\nb"),
        ("jira", "issue", "k" * 513),
        ("jira", "issue", " k"),
    ]
    for source, entity, key in cases:
        with pytest.raises(SchemaViolation) as info:
            ids.make_record_id(source, entity, key)
        assert "part" in info.value.context


def test_ut00_29_split_record_id() -> None:
    """UT00-29 the source key keeps its own colons; too few parts fail."""
    assert ids.split_record_id("monitoring:event:prometheus:abc:1") == (
        "monitoring",
        "event",
        "prometheus:abc:1",
    )
    with pytest.raises(SchemaViolation):
        ids.split_record_id("a:b")


class _Color(enum.StrEnum):
    RED = "red"


def test_ut00_30_canonical_json() -> None:
    """UT00-30 canonical encoding of supported types; everything else is rejected."""
    value = {
        "b": 1,
        "a": {"d": decimal.Decimal("12.50"), "c": datetime.datetime(2026, 9, 24, 10, tzinfo=UTC)},
        "e": datetime.date(2026, 9, 24),
        "f": pathlib.PurePosixPath("a/b"),
        "g": _Color.RED,
        "h": (1, 2),
    }
    assert ids.canonical_json(value) == (
        '{"a":{"c":"2026-09-24T10:00:00.000000Z","d":"12.50"},"b":1,'
        '"e":"2026-09-24","f":"a/b","g":"red","h":[1,2]}'
    )
    deep: Any = 1
    for _ in range(64):
        deep = [deep]
    ids.canonical_json(deep)
    bad: list[Any] = [
        float("nan"),
        {1: 2},
        {1, 2},
        b"x",
        datetime.datetime(2026, 9, 24),  # noqa: DTZ001
        [deep],
    ]
    for item in bad:
        with pytest.raises(SchemaViolation):
            ids.canonical_json(item)


def test_ut00_31_normalize_sql() -> None:
    """UT00-31 whitespace collapsed and trailing semicolons removed."""
    assert ids.normalize_sql("  SELECT\n\t1 ;; ") == "SELECT 1"
    assert ids.normalize_sql("SELECT 1") == "SELECT 1"
    assert ids.normalize_sql(";") == ""


def test_ut00_32_query_id_known_vector() -> None:
    """UT00-32 query_id equals the independently computed vector; bad inputs raise."""
    payload = json.dumps(
        {"sql": "SELECT 1", "params": {"a": 1}, "build_id": BUILD},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    expected = "q_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
    assert ids.query_id(" SELECT  1; ", {"a": 1}, BUILD) == expected
    with pytest.raises(SchemaViolation):
        ids.query_id(" ; ", {}, BUILD)
    with pytest.raises(SchemaViolation):
        ids.query_id("SELECT 1", {}, "bad")


def test_ut00_33_sha256_hex() -> None:
    """UT00-33 str input is UTF-8 encoded; output is 64 lowercase hex."""
    value = ids.sha256_hex("é")
    assert value == ids.sha256_hex("é".encode())
    assert len(value) == 64
    assert value == value.lower()


def test_ut00_34_new_token() -> None:
    """UT00-34 token sizes and bounds."""
    assert len(ids.new_token()) == 43
    assert len(ids.new_token(16)) == 22
    for bad in (15, 65):
        with pytest.raises(SchemaViolation):
            ids.new_token(bad)


_sources = st.from_regex(r"[a-z][a-z0-9_]{0,31}", fullmatch=True)
_entities = st.from_regex(r"[a-z][a-z0-9_]{0,63}", fullmatch=True)
_keys = st.text(
    alphabet=st.characters(min_codepoint=33, max_codepoint=126), min_size=1, max_size=512
)


@given(_sources, _entities, _keys)
def test_pt00_03_record_id_round_trip(source: str, entity: str, key: str) -> None:
    """PT00-03 split_record_id inverts make_record_id."""
    assert ids.split_record_id(ids.make_record_id(source, entity, key)) == (source, entity, key)


_json = st.recursive(
    st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10)),
    lambda children: st.dictionaries(st.text(max_size=5), children, max_size=4),
    max_leaves=10,
)


@given(st.dictionaries(st.text(max_size=5), _json, max_size=5), st.randoms())
def test_pt00_04_key_order_irrelevant(value: dict[str, Any], rnd: Any) -> None:
    """PT00-04 shuffling key insertion order gives identical output."""
    items = list(value.items())
    rnd.shuffle(items)
    out = ids.canonical_json(value)
    assert ids.canonical_json(dict(items)) == out
    assert json.loads(out) == value


@given(
    st.from_regex(r"SELECT [a-z]{1,8} FROM [a-z]{1,8}", fullmatch=True),
    st.sampled_from([" ", "  ", "\n", "\t "]),
    st.integers(min_value=0, max_value=3),
)
def test_pt00_05_query_id_whitespace_invariant(sql: str, ws: str, semis: int) -> None:
    """PT00-05 whitespace and trailing semicolons do not change query_id; build does."""
    noisy = ws + sql.replace(" ", ws + " ") + ws + ";" * semis
    assert ids.query_id(noisy, {}, BUILD) == ids.query_id(sql, {}, BUILD)
    assert ids.query_id(sql, {}, BUILD) != ids.query_id(sql, {}, "20260924-211404-ABCDEF")


@pytest.mark.usefixtures("fresh_ulid_state")
def test_ft00_02_clock_backwards(monkeypatch: pytest.MonkeyPatch) -> None:
    """FT00-02 a clock moving backwards still yields an increasing ULID."""
    _fixed_ns(monkeypatch, FIXED_MS * 1_000_000, (FIXED_MS - 5) * 1_000_000)
    first = ids.new_ulid()
    second = ids.new_ulid()
    assert second > first


def test_st00_05_tokens_unique_and_unpredictable() -> None:
    """ST00-05 100,000 tokens are unique and not near-sequential."""
    tokens = [ids.new_token() for _ in range(100_000)]
    assert len(set(tokens)) == len(tokens)
    values = [int.from_bytes(base64.urlsafe_b64decode(t + "=")) for t in tokens[:1000]]
    for left, right in itertools.pairwise(values):
        assert abs(left - right) >= 2**64


def test_st00_17_query_id_distinguishes_types_and_order() -> None:
    """ST00-17 1, "1", 1.0 and list order give distinct query IDs."""
    produced = {
        ids.query_id("SELECT 1", params, BUILD) for params in ({"a": 1}, {"a": "1"}, {"a": 1.0})
    }
    assert len(produced) == 3
    assert ids.query_id("SELECT 1", {"a": [1, 2]}, BUILD) != ids.query_id(
        "SELECT 1", {"a": [2, 1]}, BUILD
    )
