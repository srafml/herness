"""IT09-10, IT09-11: every dashboard page over a scored build and with no CURRENT (F09-02).

Parametrised over ``tests.support.app_env.APP_PAGES``; T09-16 - T09-19 append their pages.
Spec 11's ``tiny_build`` is not on this base: the scored ``metrics_tiny`` build planted by
``plant_scored_build`` stands in (T09-14 carry-over). Tests load no redaction key, so the
fixture installs ``install_redaction_double`` (T09-16: titles pass ``redact_text``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import cast

import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_PAGES,
    FAILED_JOB_ID,
    HIDDEN_ERROR_TEXT,
    NO_BUILD_TEXT,
    RUN_ID,
    PageSpec,
    install_redaction_double,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
    seed_home_ops,
)
from tests.support.ops_store import OpsStoreHandle

from app.common import data, wh
from herness.core.resilience import ProcessState
from herness.store.ops import bind_core_backends

pytestmark = pytest.mark.integration

TIMEOUT_S = 60
RUNNING_JOB = "job_01J0000000000000000000000C"
SNAPSHOT = {
    "workers": [],
    "running": [
        {"job_id": RUNNING_JOB, "kind": "review", "attempt": 1, "max_attempts": 3,
         "lease_left_s": 125.0, "slot": "cpu", "heartbeat_note": None}
    ],
}  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> SimpleNamespace:
    """A migrated ops store with the jobs/ops backends bound and a reviewer session.

    ``status_snapshot`` needs the chain registry and the GPU state of a configured worker host
    (impl 08 tests cover it); here it is a fixed snapshot with one running job, no worker.
    """
    del reset_process_state
    bind_core_backends()
    reads = cast("dict[str, object]", data.OPS_READS)
    monkeypatch.setitem(reads, "status_snapshot", lambda: SNAPSHOT)
    services = page_services(ops_store.data_root)
    install_session(monkeypatch, services)
    titles = install_redaction_double(monkeypatch)  # T09-16: tests load no redaction key
    return SimpleNamespace(data_root=ops_store.data_root, services=services, titles=titles)


STUB_PAGE = PageSpec(
    Path(__file__).resolve().parents[2] / "support" / "app_pages" / "stub_warehouse_page.py",
    needs_warehouse=True,
    key_texts=("Stub warehouse page",),
)
IT_PAGES = (*APP_PAGES, STUB_PAGE)  # the stub exercises the warehouse branch of IT09-11


def _values(at: AppTest) -> list[str]:
    """Values of the main area only (the sidebar carries the build caption)."""
    kinds = ("title", "subheader", "markdown", "caption", "text", "info", "warning", "success")
    return [str(e.value) for kind in kinds for e in getattr(at.main, kind)]


def _frames(at: AppTest) -> str:
    return "\n".join(d.value.to_string() for d in at.dataframe)


@pytest.mark.parametrize("spec", IT_PAGES, ids=lambda s: s.path.name)
def test_it09_10_page_renders_on_scored_build(env: SimpleNamespace, spec: PageSpec) -> None:
    """IT09-10 each page over a scored build: no exception, no error card, key elements."""
    build = plant_scored_build(env.data_root / "warehouse")
    seed_home_ops()
    at = AppTest.from_file(str(spec.path), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    shown = _values(at)
    for key in spec.key_texts:
        assert key in shown, key
    assert at.sidebar.caption[0].value == f"Build `{build}`"
    if spec.path.name == "Home.py":
        frames = _frames(at)
        assert build in frames
        assert "current" in frames
        assert RUN_ID in frames
        assert FAILED_JOB_ID in frames
        assert "QueryError" in frames
        assert RUNNING_JOB in frames
        assert "2 min" in frames  # lease left
        assert "servicenow" in frames
        assert HIDDEN_ERROR_TEXT not in frames + "\n".join(shown)
        assert "No worker is running. Start one with `herness worker`." in shown
        assert "Breaker not closed: servicenow" in shown


@pytest.mark.parametrize("spec", IT_PAGES, ids=lambda s: s.path.name)
def test_it09_11_no_current_build(env: SimpleNamespace, spec: PageSpec) -> None:
    """IT09-11 no CURRENT: warehouse pages show only the empty-state line; Overview still runs."""
    del env
    at = AppTest.from_file(str(spec.path), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    # Home's Builds block and Chat (works without a build, T09-19) show the line themselves;
    # pages 08 - 10 never read the warehouse (T09-18)
    shows_line = spec.needs_warehouse or spec.path.name in ("Home.py", "11_Chat.py")
    assert [i.value for i in at.info].count(NO_BUILD_TEXT) == int(shows_line)
    assert [c.value for c in at.sidebar.caption] == ["No promoted build"]
    if spec.needs_warehouse:
        assert [i.value for i in at.info] == [NO_BUILD_TEXT]
        assert _values(at) == [NO_BUILD_TEXT]
        assert not at.main.dataframe
        assert not at.main.expander
    else:
        shown = _values(at)
        for key in spec.key_texts:
            assert key in shown, key
