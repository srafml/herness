"""ST09-08: a denied user sees only "Not authorized" on every page and no query runs (TH09-08).

Parametrised over ``tests.support.app_env.APP_PAGES``; T09-16 - T09-19 append their pages.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_PAGES,
    PageSpec,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
    ui_config,
)

from app.common import data, wh

pytestmark = pytest.mark.unit

ELEMENTS = (
    "title", "subheader", "header", "markdown", "caption", "text", "info", "warning", "success",
    "dataframe", "table", "code", "json", "metric", "expander", "button", "selectbox",
    "text_input", "chat_input", "tabs",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.mark.parametrize("spec", APP_PAGES, ids=lambda s: s.path.name)
def test_st09_08_denied_user_sees_nothing(
    spec: PageSpec, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-08 denied user on each page: only "Not authorized"; no warehouse or ops read."""
    plant_scored_build(tmp_path / "data" / "warehouse")  # data exists, so a leak would show
    services = page_services(tmp_path / "data", ui=ui_config(default_role="denied"))
    audits = install_session(monkeypatch, services, user="mallory")
    reads: list[str] = []
    monkeypatch.setattr(wh, "get_conn", lambda *a, **k: reads.append("get_conn"))
    monkeypatch.setattr(wh, "query", lambda *a, **k: reads.append("query"))
    monkeypatch.setattr(data, "ops_read", lambda *a, **k: reads.append("ops_read"))
    at = AppTest.from_file(str(spec.path), default_timeout=60).run()
    assert not at.exception
    assert [e.value for e in at.error] == ["Not authorized"]
    for root in (at.main, at.sidebar):
        for kind in ELEMENTS:
            assert not getattr(root, kind), kind
    assert reads == []
    assert audits == [
        {"event": "auth", "user_ref": audits[0]["user_ref"], "role": "denied", "result": "denied"}
    ]
