"""Security test for retry execution (impl 08 ST08-04; TH08-04; T08-07): a huge Retry-After
never parks a call. The job-layer requeue at `now + retry_after` is checked with the queue
card (T08-12); here the call re-raises at once with the clamped `retry_after`."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_keyring import MemoryKeyring

from herness.core import config as c
from herness.core.errors import RateLimited
from herness.core.resilience import ProcessState, retry_call

pytestmark = pytest.mark.unit


@pytest.fixture
def slept(
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    fake_clock: FakeClock,
) -> Iterator[list[float]]:
    """The full test config, a fake clock and the list of every retry sleep."""
    del fake_keyring, fake_clock
    c.init_config("local", config_dir=write_full_config(tmp_path), env={})
    sleeps: list[float] = []
    reset_process_state.sleep = sleeps.append
    yield sleeps
    c.reset_config()


def _limited(retry_after: str) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://jira.example.test/rest/api/2/search")
    response = httpx.Response(429, headers={"Retry-After": retry_after}, request=request)
    return httpx.HTTPStatusError("429 Too Many Requests", request=request, response=response)


@pytest.mark.parametrize("form", ["seconds", "http_date"])
def test_st08_04_huge_retry_after_reraises_at_once(
    slept: list[float], fake_clock: FakeClock, form: str
) -> None:
    """ST08-04 `Retry-After: 86400` and an HTTP date a year ahead: the call re-raises after one
    attempt without sleeping; retry_after is 86 400 s (the year is clamped by
    retry_after_max_s), so the job layer can reschedule at now + 86 400 s."""
    year_ahead = format_datetime(fake_clock.now() + timedelta(days=365), usegmt=True)
    header = "86400" if form == "seconds" else year_ahead
    calls: list[int] = []

    def page() -> None:
        calls.append(1)
        raise _limited(header)

    with pytest.raises(RateLimited) as info:
        retry_call("source_http_page", page)
    assert (len(calls), slept) == (1, [])
    assert info.value.retry_after == 86400
    assert isinstance(info.value.__cause__, httpx.HTTPStatusError)
