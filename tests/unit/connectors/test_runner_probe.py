"""Tests for the source breaker half-open probe through the sync path (T01-27; U01-39 step 1,
U08-24 step 4): the pre-flight check never claims the probe, the request-level guard does."""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest
from tests.support.config_tree import write_full_config
from tests.support.fake_clock import FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors._runner_data import FakeConnector, make_runner, servicenow_cfg

from herness.core import config as c
from herness.core import resilience
from herness.core.errors import CircuitOpen
from herness.core.resilience import breaker
from herness.core.resilience.ports import HealthRow
from herness.store.ops import bind_core_backends, set_watermark
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

_SRC, _ENT = "servicenow", "incident"


class _ProbedConnector(FakeConnector):
    """A connector whose page fetch runs the request-level guard (as `retry_page` does)."""

    def sync(self, entity, since, until=None):  # type: ignore[no-untyped-def]
        resilience.guard(_SRC)
        yield from super().sync(entity, since, until)


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: object,
    fake_keyring: MemoryKeyring,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> Iterator[FakeClock]:
    """Ops store bound as the breaker backend, the full config (source cooldown 300 s)."""
    del reset_process_state, fake_keyring
    bind_core_backends()
    c.init_config("local", config_dir=write_full_config(tmp_path / "cfgroot"), env={})
    yield fake_clock
    c.reset_config()


def _open_breaker(clock: FakeClock) -> None:
    now = clock.now()
    row = HealthRow(_SRC, "open", 8, 1, now, "SourceUnavailable", now)
    SqliteResilienceBackend().health_apply(_SRC, lambda _before: row, now)
    breaker(_SRC).state()  # warm the cache with the open row


def test_cv_t01_27_open_breaker_before_cooldown_skips_without_request(
    env: FakeClock, ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """T01-27 an open breaker still skips the source with no request until the cooldown."""
    set_watermark(_SRC, _ENT, "sys_updated_on", env.now() - timedelta(days=1), now=env.now())
    _open_breaker(env)
    env.advance(299)
    conn = _ProbedConnector()
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root, clock=env.now)

    with pytest.raises(CircuitOpen):
        runner.run_incremental(_ENT)

    assert (conn.calls, breaker(_SRC).state()) == ([], "open")


def test_cv_t01_27_probe_after_cooldown_is_sent_once_and_closes_the_breaker(
    env: FakeClock, ops_store: OpsStoreHandle, lake: FakeLake
) -> None:
    """T01-27 after the cooldown exactly one real request goes out (the stream pre-flight
    check does not take the probe), and its success closes the breaker."""
    set_watermark(_SRC, _ENT, "sys_updated_on", env.now() - timedelta(days=1), now=env.now())
    _open_breaker(env)
    env.advance(300)
    conn = _ProbedConnector()
    runner = make_runner(conn, servicenow_cfg(), lake, ops_store.data_root, clock=env.now)

    runner.run_incremental(_ENT)
    assert (len(conn.calls), breaker(_SRC).state()) == (1, "half_open")  # the probe is in flight

    breaker(_SRC).record_success()
    assert breaker(_SRC).state() == "closed"
