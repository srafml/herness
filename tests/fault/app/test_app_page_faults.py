"""FT09-05: one named query raising on the Overview: that block shows an error card, the rest
renders (design §6; U09-66)."""

from __future__ import annotations

from collections.abc import Iterator
from typing import cast

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_DIR,
    RUN_ID,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
    seed_home_ops,
)
from tests.support.ops_store import OpsStoreHandle

from app.common import data, wh
from herness.core.errors import ConfigError, QueryError
from herness.core.resilience import ProcessState
from herness.store.ops import bind_core_backends

pytestmark = pytest.mark.fault

BROKEN = "home.dq_failed"


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.fixture
def home(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> str:
    """Scored build, seeded ops store, bound backends, a fixed snapshot; returns the build id."""
    del reset_process_state
    bind_core_backends()
    reads = cast("dict[str, object]", data.OPS_READS)
    monkeypatch.setitem(reads, "status_snapshot", lambda: {"workers": [], "running": []})
    build = plant_scored_build(ops_store.data_root / "warehouse")
    seed_home_ops()
    install_session(monkeypatch, page_services(ops_store.data_root))
    return build


def _run() -> AppTest:
    return AppTest.from_file(str(APP_DIR / "Home.py"), default_timeout=60).run()


def _cards(at: AppTest) -> list[str]:
    return [e.value.split("\n")[0] for e in at.error]


def test_ft09_05_snapshot_read_once_failure_isolated(
    home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT09-05 status_snapshot is read once per render; its failure cards Jobs and Workers only."""
    calls: list[str] = []
    real = data.ops_read

    def spy(name: str, /, **params: object) -> object:
        calls.append(name)
        if name == "status_snapshot":
            msg = "snapshot failed"
            raise QueryError(msg)
        return real(name, **params)

    monkeypatch.setattr(data, "ops_read", spy)
    at = _run()
    assert not at.exception
    assert calls.count("status_snapshot") == 1
    assert _cards(at) == ["**Jobs could not load**", "**Workers could not load**"]
    frames = "\n".join(d.value.to_string() for d in at.dataframe)
    assert home in frames
    assert RUN_ID in frames
    assert "servicenow" in frames


def test_ft09_05_sidebar_warehouse_failure_not_reported_as_no_build(
    home: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FT09-05 get_conn failing (not NoCurrentBuild): no "No promoted build" text, one card per
    warehouse block, none from the sidebar; ops blocks render."""
    del home

    def broken() -> object:
        msg = "warehouse could not be hardened"
        raise ConfigError(msg)

    monkeypatch.setattr(wh, "get_conn", broken)
    at = _run()
    assert not at.exception
    assert _cards(at) == ["**Builds could not load**", "**Data quality could not load**"]
    assert [c.value for c in at.sidebar.caption] == ["Build information unavailable"]
    shown = [str(e.value) for kind in ("info", "caption") for e in getattr(at.main, kind)]
    assert not [s for s in shown if "No promoted build" in s]
    assert RUN_ID in "\n".join(d.value.to_string() for d in at.dataframe)


def test_ft09_05_one_query_fails_rest_renders(home: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """FT09-05 `home.dq_failed` raises QueryError: "Data quality" error card, others render."""
    build = home
    real = wh.query

    def flaky(name: str, /, **params: object) -> wh.QueryResult:
        if name == BROKEN:
            msg = f"query {name} failed"
            raise QueryError(msg)
        return real(name, **params)

    monkeypatch.setattr(wh, "query", flaky)
    at = AppTest.from_file(str(APP_DIR / "Home.py"), default_timeout=60).run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.error[0].value.startswith(f"**Data quality could not load**\n\nError: query {BROKEN}")
    assert "Traceback" not in at.error[0].value
    frames = "\n".join(d.value.to_string() for d in at.dataframe)
    assert build in frames  # Builds rendered
    assert RUN_ID in frames  # Runs rendered
    assert "servicenow" in frames  # Sources rendered
    assert "No worker is running. Start one with `herness worker`." in [i.value for i in at.info]
    assert not [s.value for s in at.success]  # no "All checks passed" from the broken block
