"""Tests for tools.synth.fetch (U11-17): UT11-20, UT11-21."""

from datetime import UTC, date, datetime, timedelta
from typing import Any

import numpy as np
import pytest

from tools.synth.fetch import assign_fetch
from tools.synth.params import SynthParams, load_params

pytestmark = pytest.mark.unit

_END = date(2024, 3, 31)
_DAY = timedelta(days=1)


def _params(fetch_mode: str) -> SynthParams:
    return load_params(
        "tiny",
        start=date(2024, 1, 1),
        end=_END,
        sources=("servicenow",),
        dirty="default",
        fetch_mode=fetch_mode,  # type: ignore[arg-type]
        params_file=None,
    )


def _at(record: dict[str, Any]) -> datetime:
    value: datetime = record["at"]
    return value


def _records(n: int, offset: int = 0) -> list[dict[str, Any]]:
    base = datetime(2024, 1, 1, tzinfo=UTC)
    return [{"n": offset + i, "at": base + timedelta(hours=13 * i)} for i in range(n)]


def test_ut11_20_initial_background_on_end_plus_one() -> None:
    """UT11-20 initial: background `_fetched_at` lies on day end + 1; re-emits on days
    end + 2 .. end + 15, every day of that range used."""
    records, reemits = _records(2_000), _records(3_000, offset=10_000)
    rng = np.random.default_rng(20)
    out = assign_fetch(records, reemits, _params("initial"), rng, updated_at_of=_at)
    assert [r for r, _ in out] == [*records, *reemits]
    first = datetime.combine(_END + _DAY, datetime.min.time(), UTC)
    for _, fetched in out[: len(records)]:
        assert first <= fetched < first + _DAY
        assert fetched.tzinfo is not None
    days = {(fetched.date() - _END).days for _, fetched in out[len(records) :]}
    assert days == set(range(2, 16))


def test_ut11_20_initial_is_deterministic() -> None:
    """UT11-20 the same seed gives the same `_fetched_at` values."""
    records, reemits = _records(50), _records(20, offset=100)
    runs = [
        assign_fetch(
            records, reemits, _params("initial"), np.random.default_rng(4), updated_at_of=_at
        )
        for _ in range(2)
    ]
    assert runs[0] == runs[1]


def test_ut11_21_daily_within_six_hours() -> None:
    """UT11-21 daily: 0 <= `_fetched_at` - `_source_updated_at` <= 6 h for every record."""
    records, reemits = _records(2_000), _records(200, offset=5_000)
    rng = np.random.default_rng(21)
    out = assign_fetch(records, reemits, _params("daily"), rng, updated_at_of=_at)
    assert [r for r, _ in out] == [*records, *reemits]
    gaps = [fetched - _at(record) for record, fetched in out]
    assert all(timedelta(0) <= gap <= timedelta(hours=6) for gap in gaps)
    assert max(gaps) > timedelta(hours=5)


def test_ut11_21_empty_input() -> None:
    """UT11-21 no records gives no rows."""
    rng = np.random.default_rng(0)
    assert assign_fetch([], [], _params("daily"), rng, updated_at_of=_at) == []
    assert assign_fetch([], [], _params("initial"), rng, updated_at_of=_at) == []
