"""IT09-17: the dashboard follows CURRENT when a new build is promoted mid-session (F09-10)."""

from __future__ import annotations

from collections.abc import Iterator

import duckdb
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from structlog.testing import capture_logs
from tests.support.app_env import preserved_main, wh_script, wh_services
from tests.support.fake_clock import FIXTURE_START, FakeClock
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import build_id, make_build

from app.common import bootstrap, wh
from herness.core import time as clock
from herness.model.promote import promote_build
from herness.model.settings import BuildSettings
from herness.store._warehouse_rw import write_current
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

OLD, NEW = build_id(1), build_id(2)


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    wh._pool.clear()


def test_it09_17_promotion_switches_session(
    monkeypatch: pytest.MonkeyPatch, ops_store: OpsStoreHandle
) -> None:
    """IT09-17 promote a second build mid-session; after 60 s the page shows it, with a toast."""
    fc = FakeClock(FIXTURE_START)
    monkeypatch.setattr(clock, "now", fc.now)
    layout = DataLayout.from_root(ops_store.data_root)
    make_build(layout, OLD, "promoted")
    write_current(OLD, layout=layout)
    services = wh_services(layout.root)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)

    at = AppTest.from_function(wh_script).run()
    assert [t.value for t in at.text] == [OLD]
    old_conn = wh._pool().get(OLD, layout)

    make_build(layout, NEW, "building")
    con = duckdb.connect(str(layout.warehouse / f"wh-{NEW}.duckdb"))
    promote_build(con, NEW, build_cfg=BuildSettings(keep_last=2), layout=layout, now=fc.now())

    fc.advance(30)
    at.run()
    assert [t.value for t in at.text] == [OLD]  # not rechecked yet
    fc.advance(30)
    with capture_logs() as logs:
        at.run()
    assert [t.value for t in at.text] == [NEW]
    assert [t.value for t in at.toast] == [f"Data refreshed to build {NEW}"]
    switched = [e for e in logs if e["event"] == "app.warehouse.switched"]
    assert [(e["log_level"], e["old_build_id"], e["new_build_id"]) for e in switched] == [
        ("info", OLD, NEW)
    ]

    fc.advance(wh.STALE_CONN_GRACE_S + 1)
    at.run()
    assert [t.value for t in at.text] == [NEW]
    with pytest.raises(duckdb.ConnectionException):
        old_conn.execute("SELECT 1")  # closed once stale for more than 300 s
