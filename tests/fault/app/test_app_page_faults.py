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
from herness.core.errors import QueryError
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


def test_ft09_05_one_query_fails_rest_renders(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT09-05 `home.dq_failed` raises QueryError: "Data quality" error card, others render."""
    del reset_process_state
    bind_core_backends()
    reads = cast("dict[str, object]", data.OPS_READS)
    monkeypatch.setitem(reads, "status_snapshot", lambda: {"workers": [], "running": []})
    build = plant_scored_build(ops_store.data_root / "warehouse")
    seed_home_ops()
    install_session(monkeypatch, page_services(ops_store.data_root))
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
