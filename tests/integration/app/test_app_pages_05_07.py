"""IT09-10 (pages 05 - 07): Clusters, Change Health and Delivery Health over a scored build.

The scored ``metrics_tiny`` build (``plant_scored_build``) has no cluster row, no redacted text
and no delivery metric; ``_plant_extras`` adds one cluster with a Markdown-bearing label, the
redacted text of its members, raw text in ``core.incident`` that must never show (TH09-11) and
``throughput`` values. Derived and redacted text appear only as dataframe cells (TH09-02).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from tests.support.app_env import (
    APP_DIR,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
)

from app.common import wh

pytestmark = pytest.mark.integration

TIMEOUT_S = 60
PAGES = APP_DIR / "pages"
LABEL = "Disk **full** [click](http://evil.example) on hosts"
RAW_TEXT = "RAW ticket text alice@example.com"
REDACTED = "Disk full on host [HOST] for [EMAIL]"
TEXT_KINDS = (
    "title", "subheader", "header", "markdown", "caption", "text", "info", "warning", "success",
    "error", "code", "json",
)  # fmt: skip


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


def _plant_extras(warehouse: Path, build_id: str) -> None:
    con = duckdb.connect(str(warehouse / f"wh-{build_id}.duckdb"))
    try:
        con.execute(
            "INSERT INTO enrich.cluster (cluster_id, label, root_cause_category, size,"
            " first_seen, last_seen, top_terms, service_ids, algorithm_version) VALUES"
            " ('C1', ?, 'capacity', 4, TIMESTAMPTZ '2026-01-05 00:00:00+00',"
            " TIMESTAMPTZ '2026-02-20 00:00:00+00', ['disk', 'full'], ['S1'], 'v1')",
            [LABEL],
        )
        con.execute("UPDATE core.incident SET short_description = ?, description = ?",
                    [RAW_TEXT, RAW_TEXT])  # fmt: skip
        con.execute(
            "INSERT INTO enrich.text_redacted (record_id, entity, text, content_hash)"
            " SELECT record_id, 'incident', ?, 'h' FROM core.incident",
            [REDACTED],
        )
        con.execute(
            "INSERT INTO metrics.metric_value (metric, entity_type, entity_id, period,"
            " period_start, value, numerator, denominator, sample_size, unit, flags, query_id)"
            " VALUES ('throughput', 'team', 'T1', 'month', DATE '2026-01-01', 7, 7, NULL, 7,"
            " 'count', [], 'q_0000000000000001'), ('throughput', 'team', 'T1', 'month',"
            " DATE '2026-02-01', 9, 9, NULL, 9, 'count', [], 'q_0000000000000001'),"
            " ('throughput', 'team', 'T2', 'month', DATE '2026-02-01', 3, 3, NULL, 3, 'count',"
            " [], 'q_0000000000000001'), ('throughput', 'work_item', 'W1', 'month',"
            " DATE '2026-02-01', 2, 2, NULL, 2, 'count', [], 'q_0000000000000001')"
        )
    finally:
        con.close()


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """A scored build with the extra rows and a reviewer session."""
    data_root = tmp_path / "data"
    build = plant_scored_build(data_root / "warehouse")
    _plant_extras(data_root / "warehouse", build)
    install_session(monkeypatch, page_services(data_root))
    return SimpleNamespace(build=build)


def _run(name: str) -> AppTest:
    at = AppTest.from_file(str(PAGES / name), default_timeout=TIMEOUT_S).run()
    assert not at.exception
    assert not at.error, [e.value for e in at.error]
    return at


def _texts(at: AppTest) -> str:
    """Every non-dataframe value on the page (Markdown-capable elements included)."""
    return "\n".join(str(e.value) for k in TEXT_KINDS for e in getattr(at, k))


def _frame_texts(at: AppTest) -> list[str]:
    return [d.value.to_string() for d in at.dataframe]


def _frame(at: AppTest, column: str) -> list[object]:
    """Values of ``column`` in the first dataframe that has it."""
    frame = next(d.value for d in at.dataframe if column in d.value.columns)
    return list(frame[column])


def test_it09_10_clusters_list_detail_and_redacted_members(env: SimpleNamespace) -> None:
    """IT09-10 Clusters: list, label and terms, monthly chart, 4 members with redacted text."""
    del env
    at = _run("05_Clusters.py")
    assert _frame(at, "label")[0] == LABEL  # derived text only as a dataframe cell
    assert _frame(at, "top_terms")[0] == "disk, full"
    assert at.selectbox(key="clusters_detail").value == "C1"
    assert _frame(at, "number") == ["INC0001", "INC0004", "INC0002", "INC0003"]
    assert _frame(at, "text") == [REDACTED] * 4
    assert at.get("arrow_vega_lite_chart") or at.get("vega_lite_chart")
    shown = _texts(at)
    assert REDACTED not in shown
    assert "evil.example" not in shown
    assert "**full**" not in shown
    assert RAW_TEXT not in shown + "\n".join(_frame_texts(at))


def test_it09_10_clusters_filters_narrow_the_list(env: SimpleNamespace) -> None:
    """IT09-10 Clusters: min size above every cluster empties list and detail."""
    del env
    at = _run("05_Clusters.py")
    assert at.number_input(key="clusters_min_size").value == 1
    at.number_input(key="clusters_min_size").set_value(5).run()
    assert not at.error
    assert "No clusters match the filters." in [i.value for i in at.info]
    assert not any("label" in d.value.columns for d in at.dataframe)
    at.number_input(key="clusters_min_size").set_value(1).run()
    at.selectbox(key="clusters_root_cause").set_value("capacity").run()
    at.selectbox(key="clusters_service").set_value("S2").run()
    assert "No clusters match the filters." in [i.value for i in at.info]


def test_it09_10_change_health_metrics_and_caused_incidents(env: SimpleNamespace) -> None:
    """IT09-10 Change Health: change metrics, link counts by method, identifiers only."""
    del env
    at = _run("06_Change_Health.py")
    assert at.selectbox(key="change_period").value == "month"
    assert set(_frame(at, "metric")) == {"change_failure_rate"}
    assert _frame(at, "method") == ["time_window"]
    assert _frame(at, "links") == [3]
    assert sorted(_frame(at, "change_id")) == ["CH1", "CH1", "CH4"]
    assert "INC0002" in _frame(at, "number")
    assert at.get("arrow_vega_lite_chart") or at.get("vega_lite_chart")
    assert RAW_TEXT not in _texts(at) + "\n".join(_frame_texts(at))
    at.selectbox(key="change_service").set_value("S2").run()
    assert not at.error
    assert set(_frame(at, "entity_id")) == {"S2"}
    assert sorted(_frame(at, "change_id")) == ["CH1", "CH4"]


def test_it09_10_delivery_health_metrics_by_team_and_project(env: SimpleNamespace) -> None:
    """IT09-10 Delivery Health: delivery metrics, team and project filters."""
    del env
    at = _run("07_Delivery_Health.py")
    assert set(_frame(at, "metric")) == {"throughput"}
    assert set(_frame(at, "entity_id")) == {"T1", "T2", "W1"}
    assert at.get("arrow_vega_lite_chart") or at.get("vega_lite_chart")
    at.selectbox(key="delivery_team").set_value("T1").run()
    assert not at.error
    assert set(_frame(at, "entity_id")) == {"T1"}
    at.selectbox(key="delivery_team").set_value(None).run()
    at.selectbox(key="delivery_project").set_value("PAY").run()
    assert set(_frame(at, "entity_id")) == {"T1", "T2", "W1"}
    at.selectbox(key="delivery_period").set_value("week").run()
    assert "No metric values for these filters." in [i.value for i in at.info]
