"""UT09-120: the cached metric view of pages 06 and 07 is keyed by build id and page inputs."""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

import pyarrow as pa
import pytest
import streamlit as st

from app.common import _pages_05_07 as shared
from app.common.wh import QueryResult

pytestmark = pytest.mark.unit

TABLE = pa.Table.from_pylist(
    [
        {"metric": "m", "entity_type": "team", "entity_id": "t1", "period_start": "2026-01-01",
         "value": 1.5, "sample_size": 3, "unit": "d", "flags": None},
        {"metric": "m", "entity_type": "team", "entity_id": "t2", "period_start": "2026-01-01",
         "value": 2.5, "sample_size": 4, "unit": "d", "flags": None},
    ]
)  # fmt: skip


@dataclass
class Fake:
    """The current build of a fake warehouse and the builds the view was computed for."""

    build: str = "b1"
    computed: list[str] = field(default_factory=list)


@pytest.fixture(autouse=True)
def _fresh() -> Iterator[None]:
    st.cache_data.clear()
    yield
    st.cache_data.clear()


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> Fake:
    """A fake ``metrics.values`` reporting ``Fake.build``; every real computation is logged."""
    state = Fake()

    def query(name: str, /, **params: object) -> QueryResult:
        assert name == "metrics.values", params
        return QueryResult(TABLE, False, state.build)

    real = shared.metric_view_data

    def spy(table: pa.Table, names: tuple[str, ...]) -> shared.MetricView:
        state.computed.append(state.build)
        return real(table, names)

    monkeypatch.setattr(shared.wh, "query", query)
    monkeypatch.setattr(shared, "metric_view_data", spy)
    monkeypatch.setattr(shared, "metric_names", lambda domain: ("m",))
    return state


def test_ut09_120_same_build_same_inputs_hits(fake: Fake) -> None:
    """UT09-120 same build and same inputs: the second call is a cache hit."""
    first = shared.metric_view("change", "month", {"entity_id": "t1"})
    second = shared.metric_view("change", "month", {"entity_id": "t1"})
    assert fake.computed == ["b1"]
    assert first == second


def test_ut09_120_other_build_misses(fake: Fake) -> None:
    """UT09-120 two builds never share an entry: the key carries the build id."""
    shared.metric_view("change", "month", {})
    fake.build = "b2"
    shared.metric_view("change", "month", {})
    assert fake.computed == ["b1", "b2"]
    fake.build = "b1"
    shared.metric_view("change", "month", {})
    assert fake.computed == ["b1", "b2"]  # b1's own entry is still served


def test_ut09_120_other_period_or_filter_or_domain_misses(fake: Fake) -> None:
    """UT09-120 a different period, filter or domain is a new entry."""
    shared.metric_view("change", "month", {})
    shared.metric_view("change", "week", {})
    shared.metric_view("change", "month", {"entity_id": "t1"})
    shared.metric_view("delivery", "month", {})
    assert len(fake.computed) == 4


def test_ut09_120_cached_value_holds_no_raw_text(fake: Fake) -> None:
    """UT09-120 the cached view holds table records, ids and chart specs only."""
    view = shared.metric_view("change", "month", {})
    assert fake.computed == ["b1"]
    assert [r["metric"] for r in view.records] == ["m", "m"]
    assert [metric for metric, _ in view.charts] == ["m"]
    assert set(view.records[0]) == set(shared.METRIC_COLUMNS)
