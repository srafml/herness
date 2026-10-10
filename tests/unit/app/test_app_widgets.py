"""UT09-73, UT09-74, UT09-84, UT09-96: page wrapper, block, evidence widget, helpers (T09-15)."""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pyarrow as pa
import pytest
import streamlit as st
from streamlit.testing.v1 import AppTest
from structlog.testing import capture_logs
from tests.support.app_env import (
    NO_BUILD_TEXT,
    install_session,
    page_services,
    plant_scored_build,
    preserved_main,
    ui_config,
)

from app.common import auth, bootstrap, data, wh, widgets
from app.common.sanitize import safe_markdown
from app.common.wh import NoCurrentBuild, QueryResult
from herness.core.errors import ConfigError, FatalError, QueryError, StoreBusy

pytestmark = pytest.mark.unit

Q1, Q2, Q3 = "q_" + "1" * 16, "q_" + "2" * 16, "q_" + "3" * 16


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    wh._pool.clear()
    with preserved_main():
        yield
    st.cache_data.clear()
    wh._pool.clear()


@pytest.fixture
def services(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """Alice (reviewer) on an empty data dir (no CURRENT)."""
    svc = page_services(tmp_path / "data")
    install_session(monkeypatch, svc)
    return svc


def _all_text(at: AppTest) -> str:
    """Every rendered value of the main area and sidebar, joined (for absence checks)."""
    kinds = ("markdown", "caption", "text", "error", "info", "warning", "success", "subheader")
    parts: list[str] = []
    for root in (at.main, at.sidebar):
        for kind in kinds:
            parts += [str(e.value) for e in getattr(root, kind)]
    parts += [e.label for e in at.expander]
    return "\n".join(parts)


def _page_texts(at: AppTest) -> list[str]:
    """Texts written by the page body or after the wrapper (not the error Details lines)."""
    return [t.value for t in at.text if t.value.startswith(("body", "after"))]


# --- scripts (AppTest runs each function body as a page script) --------------------------------


def body_script(kind: str, needs_warehouse: bool = False) -> None:
    import streamlit as st  # noqa: PLC0415

    from app.common.widgets import PageContext, page  # noqa: PLC0415
    from herness.core.errors import QueryError  # noqa: PLC0415

    def body(ctx: PageContext) -> None:
        st.text(f"body {ctx.actor.role} {ctx.build_id}")
        if kind == "herness":
            msg = "query x failed"
            raise QueryError(msg, details={"run_id": "run_1", "job_id": "job_2"})
        if kind == "value":
            msg = "internal detail 42"
            raise ValueError(msg)
        if kind == "stop":
            st.stop()
        st.text("body end")

    page("Test Page", body, needs_warehouse=needs_warehouse)
    st.text("after page")


def blocks_script(kind: str) -> None:
    import streamlit as st  # noqa: PLC0415

    from app.common.widgets import PageContext, block, page  # noqa: PLC0415
    from herness.core.errors import StoreBusy  # noqa: PLC0415

    def body(ctx: PageContext) -> None:
        del ctx
        with block("Alpha"):
            st.text("alpha start")
            if kind == "herness":
                msg = "ops store busy"
                raise StoreBusy(msg)
            msg = "internal 99"
            raise KeyError(msg)
        with block("Beta"):
            st.text("beta renders")

    page("Blocks", body, needs_warehouse=False)


def evidence_script(ids: list[str], label: str | None = None) -> None:
    from app.common.widgets import evidence  # noqa: PLC0415

    evidence(ids, label=label)


def evidence_in_block_script(ids: list[str]) -> None:
    import streamlit as st  # noqa: PLC0415

    from app.common.widgets import block, evidence  # noqa: PLC0415

    with block("Evidence"):
        evidence(ids)
    st.text("after")


def helpers_script(status: str | None) -> None:
    from typing import cast  # noqa: PLC0415

    from app.common.widgets import banner_strip, verification_badge  # noqa: PLC0415
    from herness.reports._data import ReportBanner  # noqa: PLC0415

    verification_badge(cast("Any", status))
    banner_strip([ReportBanner("dq_warnings", "Some checks failed.", ("a_1", "b <x>"))])


def error_script(kind: str) -> None:
    from app.common.widgets import user_error  # noqa: PLC0415
    from herness.core.errors import ModelUnavailable, QueryError  # noqa: PLC0415

    if kind == "model":
        user_error(ModelUnavailable("down", details={"url": "http://m:8000"}))
    else:
        user_error(QueryError("query x failed", details={"run_id": "run_7"}), title="Block X")


# --- UT09-73 page -------------------------------------------------------------------------------


def test_ut09_73_herness_error_shows_error_card(services: SimpleNamespace) -> None:
    """UT09-73 body raising HernessError: Error/Fix card, Details expander, page continues."""
    del services
    at = AppTest.from_function(body_script, args=("herness",)).run()
    assert not at.exception
    what, fix = widgets.user_message(QueryError("query x failed"))
    assert [e.value for e in at.error] == [
        f"Error: {safe_markdown(what)}\n\nFix: {safe_markdown(fix)}"
    ]
    assert what == "query x failed"
    details = [e for e in at.expander if e.label == "Details"]
    assert len(details) == 1
    shown = [t.value for t in details[0].text]
    assert "Error type: QueryError" in shown
    assert "run_id: run_1" in shown
    assert "job_id: job_2" in shown
    assert any(t.startswith("Log: data/logs/herness-") for t in shown)
    assert [t.value for t in at.text][-1] == "after page"
    assert "Traceback" not in _all_text(at)


def test_ut09_73_other_exception_logged_generic_card(services: SimpleNamespace) -> None:
    """UT09-73 body raising ValueError: generic card, `app.page.failed` logged, no detail shown."""
    del services
    with capture_logs() as logs:
        at = AppTest.from_function(body_script, args=("value",)).run()
    assert not at.exception
    assert len(at.error) == 1
    card = at.error[0].value
    assert card.startswith("Error: Unexpected error on this page.")
    assert "Fix: " in card
    assert "internal detail 42" not in _all_text(at)
    failed = [e for e in logs if e["event"] == "app.page.failed"]
    assert [(e["log_level"], e["page"], e["error_type"]) for e in failed] == [
        ("error", "Test Page", "ValueError")
    ]
    rendered = [e for e in logs if e["event"] == "app.page.rendered"]
    assert len(rendered) == 1
    assert rendered[0]["page"] == "Test Page"
    assert set(rendered[0]) >= {"duration_ms", "build_id"}


def test_ut09_73_stop_propagates(services: SimpleNamespace) -> None:
    """UT09-73 body calling st.stop(): no error card, the script stops (StopException re-raised)."""
    del services
    at = AppTest.from_function(body_script, args=("stop",)).run()
    assert not at.exception
    assert not at.error
    texts = [t.value for t in at.text]
    assert "body end" not in texts
    assert "after page" not in texts


def test_ut09_73_happy_path_context_and_page_config(services: SimpleNamespace) -> None:
    """UT09-73 the body gets the actor; no CURRENT on a non-warehouse page: build_id None."""
    del services
    at = AppTest.from_function(body_script, args=("ok",)).run()
    assert not at.exception
    assert [t.value for t in at.text] == ["body reviewer None", "body end", "after page"]
    assert [c.value for c in at.sidebar.caption] == ["No promoted build"]


def test_ut09_73_get_services_config_error_stops(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-73 step 2: ConfigError from get_services -> error card and stop; body never runs."""

    def broken() -> object:
        msg = "config invalid"
        raise ConfigError(msg, hint="Fix config/herness.yaml.")

    monkeypatch.setattr(bootstrap, "get_services", broken)
    at = AppTest.from_function(body_script, args=("ok",)).run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.error[0].value.startswith("Error: config invalid")
    assert not _page_texts(at)


def test_ut09_73_audit_failure_fails_closed(
    services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-73 controller ruling 1: an `auth` audit failure stops the page before any read."""
    del services

    def broken(*_: object, **__: object) -> None:
        msg = "audit log not writable"
        raise FatalError(msg)

    reads: list[str] = []
    monkeypatch.setattr(auth, "audit", broken)
    monkeypatch.setattr(wh, "get_conn", lambda: reads.append("get_conn"))
    monkeypatch.setattr(wh, "query", lambda *a, **k: reads.append("query"))
    monkeypatch.setattr(data, "ops_read", lambda *a, **k: reads.append("ops_read"))
    at = AppTest.from_function(body_script, args=("ok", True)).run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.error[0].value.startswith("Error: ")
    assert "Traceback" not in _all_text(at)
    assert not _page_texts(at)
    assert reads == []


def test_ut09_73_require_role_failure_stops(
    services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-73 step 4: an exception from require_role stops the page (fail closed)."""
    del services

    def refuse(actor: object, action: str) -> None:
        msg = "boom"
        raise RuntimeError(msg)

    monkeypatch.setattr(widgets, "require_role", refuse)
    with capture_logs() as logs:
        at = AppTest.from_function(body_script, args=("ok",)).run()
    assert not at.exception
    assert [e.value.split("\n")[0] for e in at.error] == ["Error: Unexpected error on this page."]
    assert not _page_texts(at)
    failed = [e for e in logs if e["event"] == "app.page.failed"]
    assert [(e["page"], e["error_type"], e["stage"]) for e in failed] == [
        ("Test Page", "RuntimeError", "gate")
    ]
    assert not [e for e in logs if e["event"] == "app.page.gate_failed"]


@pytest.mark.parametrize("error", [ConfigError("warehouse could not be hardened"), OSError("x")])
def test_ut09_73_step6_failure_one_card_and_stop(
    services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    """UT09-73 step 6: any get_conn failure -> one error card (not one per sidebar), stop."""
    del services

    def broken() -> object:
        raise error

    monkeypatch.setattr(wh, "get_conn", broken)
    with capture_logs() as logs:
        at = AppTest.from_function(body_script, args=("ok", True)).run()
    assert not at.exception
    assert len(at.error) == 1
    assert not _page_texts(at)
    assert [c.value for c in at.sidebar.caption] == ["Build information unavailable"]
    assert "Traceback" not in _all_text(at)
    stages = [e.get("stage") for e in logs if e["event"] == "app.page.failed"]
    assert stages == ([] if isinstance(error, ConfigError) else ["warehouse"])


def test_ut09_73_metric_failure_does_not_break_page(
    services: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-73 step 8: a failing histogram record never escapes the wrapper."""
    del services

    def broken(*_: object, **__: object) -> None:
        msg = "metric"
        raise ConfigError(msg)

    monkeypatch.setattr(widgets, "record_histogram", broken)
    at = AppTest.from_function(body_script, args=("ok",)).run()
    assert not at.exception
    assert not at.error
    assert [t.value for t in at.text][-1] == "after page"


def test_ut09_73_denied_sees_only_not_authorized(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-73 / U09-65 step 3: role `denied` -> only "Not authorized"."""
    svc = page_services(tmp_path / "data", ui=ui_config(default_role="denied"))
    install_session(monkeypatch, svc, user="mallory")
    at = AppTest.from_function(body_script, args=("ok",)).run()
    assert not at.exception
    assert [e.value for e in at.error] == ["Not authorized"]
    assert not _page_texts(at)
    assert not at.sidebar.caption


def test_ut09_73_no_current_on_warehouse_page(services: SimpleNamespace) -> None:
    """UT09-73 / U09-65 step 6: no CURRENT on a warehouse page -> only the info line."""
    del services
    at = AppTest.from_function(body_script, args=("ok", True)).run()
    assert not at.exception
    assert [i.value for i in at.info] == [NO_BUILD_TEXT]
    assert not _page_texts(at)
    assert not at.error


def test_ut09_73_sidebar_build_and_d2_banner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-73 step 5: sidebar shows the current build, data-as-of and the D2 banner."""
    svc = page_services(tmp_path / "data", unconfirmed=["toil", "change"])
    install_session(monkeypatch, svc)
    build = plant_scored_build(tmp_path / "data" / "warehouse")
    at = AppTest.from_function(body_script, args=("ok", True)).run()
    assert not at.exception
    captions = [c.value for c in at.sidebar.caption]
    assert captions[0] == f"Build `{build}`"
    assert [w.value for w in at.sidebar.warning] == [
        "Dollar weights are placeholders and not yet confirmed: change, toil"
    ]
    assert at.text[0].value == f"body reviewer {build}"


# --- UT09-74 block ------------------------------------------------------------------------------


@pytest.mark.parametrize("kind", ["herness", "other"])
def test_ut09_74_block_failure_isolated(services: SimpleNamespace, kind: str) -> None:
    """UT09-74 a raising block shows an error card and the next block renders."""
    del services
    with capture_logs() as logs:
        at = AppTest.from_function(blocks_script, args=(kind,)).run()
    assert not at.exception
    texts = [t.value for t in at.text]
    assert [t for t in texts if t.startswith(("alpha", "beta"))] == ["alpha start", "beta renders"]
    assert f"Error type: {'StoreBusy' if kind == 'herness' else 'KeyError'}" in texts
    assert len(at.error) == 1
    card = at.error[0].value
    assert card.startswith("**Alpha could not load**")
    assert "Error: " in card
    assert "Fix: " in card
    assert "internal 99" not in _all_text(at)
    failed = [e for e in logs if e["event"] == "app.widget.failed"]
    error_type = "StoreBusy" if kind == "herness" else "KeyError"
    assert [(e["log_level"], e["page"], e["block"], e["error_type"]) for e in failed] == [
        ("error", "Blocks", "Alpha", error_type)
    ]
    assert not [e for e in logs if e["event"] == "app.page.failed"]


def test_ut09_74_block_reraises_control_flow() -> None:
    """UT09-74 Streamlit control-flow exceptions pass through `block`."""
    from streamlit.runtime.scriptrunner_utils.exceptions import StopException  # noqa: PLC0415

    with pytest.raises(StopException), widgets.block("X"):
        raise StopException

    class RerunException(Exception):  # noqa: N818 - matched by class name
        pass

    with pytest.raises(RerunException), widgets.block("X"):
        raise RerunException


# --- UT09-84 evidence ---------------------------------------------------------------------------


def _ops_row(qid: str, sample: object) -> dict[str, object]:
    return {
        "query_id": qid, "run_id": None, "build_id": "20260101-000000-ABCDEF",
        "sql": "SELECT 1", "result_hash": "h", "params": {"a": 1}, "result_sample": sample,
        "row_count": 3, "duration_ms": 1, "executed_at": "2026-01-01T00:00:00Z",
    }  # fmt: skip


def _meta_table(rows: list[dict[str, object]], *, with_sample: bool) -> pa.Table:
    cols = ["query_id", "sql", "params", "row_count", "executed_at"]
    cols += ["result_sample"] if with_sample else []
    return pa.Table.from_pylist([{c: r.get(c) for c in cols} for r in rows])


def _patch_evidence(
    monkeypatch: pytest.MonkeyPatch, ops: dict[str, object], meta: pa.Table | None
) -> list[tuple[str, dict[str, Any]]]:
    calls: list[tuple[str, dict[str, Any]]] = []

    def ops_read(name: str, /, **params: object) -> object:
        calls.append((name, dict(params)))
        return {k: v for k, v in ops.items() if k in params["ids"]}  # type: ignore[operator]

    def query(name: str, /, **params: object) -> QueryResult:
        calls.append((name, dict(params)))
        if meta is None:
            raise NoCurrentBuild
        return QueryResult(meta, truncated=False, build_id="20260202-000000-ABCDEF")

    monkeypatch.setattr(data, "ops_read", ops_read)
    monkeypatch.setattr(wh, "query", query)
    return calls


def test_ut09_84_ops_meta_and_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-84 ids in ops, in meta and nowhere: anchors, samples, "Evidence not found"."""
    long = "x" * 300
    ops = {Q1: _ops_row(Q1, [{"n": 1, "name": long, "description": "RAW TICKET TEXT"}])}
    meta_row = {
        "query_id": Q2, "sql": "SELECT 2", "params": json.dumps({"b": 2}), "row_count": 1,
        "executed_at": None, "result_sample": json.dumps([{"v": 2.5}]),
    }  # fmt: skip
    calls = _patch_evidence(monkeypatch, ops, _meta_table([meta_row], with_sample=True))
    at = AppTest.from_function(evidence_script, args=([Q1, Q2, Q1, "bad id", Q3],)).run()
    assert not at.exception
    assert [e.label for e in at.expander] == ["Evidence (3 queries)"]
    assert [(h.value, h.proto.anchor) for h in at.subheader] == [
        (Q1, f"ev-{Q1}"), (Q2, f"ev-{Q2}"), (Q3, f"ev-{Q3}"),
    ]  # fmt: skip
    assert [c.value for c in at.code] == ["SELECT 1", "SELECT 2"]
    assert [c.language for c in at.code] == ["sql", "sql"]
    frames = [d.value for d in at.dataframe]
    assert list(frames[0].columns) == ["n", "name"]  # raw-text column never shown
    assert frames[0].iloc[0]["name"] == "x" * 200
    assert frames[0].iloc[0]["n"] == "1"  # string cells
    assert frames[1].iloc[0]["v"] == "2.5"
    texts = [t.value for t in at.text]
    assert "Build 20260101-000000-ABCDEF · 3 rows" in texts
    assert "Build 20260202-000000-ABCDEF · 1 rows" in texts
    assert [c.value for c in at.caption] == ["Evidence not found"]
    assert "RAW TICKET TEXT" not in _all_text(at)
    assert calls[0] == ("ui_get_evidence_rows", {"ids": (Q1, Q2, Q3)})
    assert calls[1] == ("evidence.meta", {"ids": [Q2, Q3]})


def test_ut09_84_sample_not_stored(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-84 a pre-v2 build (no result_sample column): "Sample not stored for this build"."""
    meta_row = {"query_id": Q2, "sql": "SELECT 2", "params": None, "row_count": 1}
    _patch_evidence(monkeypatch, {}, _meta_table([meta_row], with_sample=False))
    at = AppTest.from_function(evidence_script, args=([Q2], "Sources")).run()
    assert not at.exception
    assert [e.label for e in at.expander] == ["Sources"]
    assert [c.value for c in at.caption] == ["Sample not stored for this build"]
    assert not at.dataframe


def test_ut09_84_no_warehouse_and_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-84 no CURRENT: missing ids are "Evidence not found"; 50 shown, then "… and N more"."""
    ids = [f"q_{i:016x}" for i in range(53)]
    calls = _patch_evidence(monkeypatch, {}, None)
    at = AppTest.from_function(evidence_script, args=(ids,)).run()
    assert not at.exception
    assert [e.label for e in at.expander] == ["Evidence (53 queries)"]
    assert len(at.subheader) == 50
    captions = [c.value for c in at.caption]
    assert captions.count("Evidence not found") == 50
    assert captions[-1] == "… and 3 more"
    assert calls[0] == ("ui_get_evidence_rows", {"ids": tuple(ids[:50])})


def test_ut09_84_errors_reach_enclosing_block(monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-84 a failing ops read raises out of the widget (the enclosing block handles it)."""

    def broken(name: str, /, **params: object) -> object:
        msg = "ops store busy"
        raise StoreBusy(msg)

    monkeypatch.setattr(data, "ops_read", broken)
    with capture_logs() as logs:
        at = AppTest.from_function(evidence_in_block_script, args=([Q1],)).run()
    assert not at.exception
    assert len(at.error) == 1
    assert at.error[0].value.startswith(
        f"**Evidence could not load**\n\nError: {safe_markdown('Ops store is locked.')}"
    )
    texts = [t.value for t in at.text]
    assert texts[0] == "Error type: StoreBusy"
    assert texts[-1] == "after"  # the StoreBusy reached the block; the script went on
    failed = [e for e in logs if e["event"] == "app.widget.failed"]
    assert [(e["block"], e["error_type"]) for e in failed] == [("Evidence", "StoreBusy")]


# --- UT09-96 presentational helpers -------------------------------------------------------------


@pytest.mark.parametrize(
    ("status", "badge", "caption"),
    [
        ("verified", ":green-badge[Verified]", None),
        (
            "partial",
            ":orange-badge[Partially verified]",
            "Some figures could not be verified and were removed.",
        ),
        ("unverified", ":gray-badge[Unverified]", None),
        (None, ":gray-badge[Unverified]", None),
    ],
)
def test_ut09_96_badges_and_banners(status: str | None, badge: str, caption: str | None) -> None:
    """UT09-96 each verification status renders its constant badge; banners as warnings."""
    at = AppTest.from_function(helpers_script, args=(status,)).run()
    assert not at.exception
    assert [m.value for m in at.markdown] == [badge]
    captions = [c.value for c in at.caption]
    assert captions[:-1] == ([caption] if caption else [])
    assert [w.value for w in at.warning] == [safe_markdown("Some checks failed.")]
    assert captions[-1] == "- a_1\n- b &lt;x&gt;"


def test_ut09_96_user_error_lines() -> None:
    """UT09-96 user_error: Error/Fix lines from user_message, title, Details with ids."""
    at = AppTest.from_function(error_script, args=("model",)).run()
    assert not at.exception
    assert at.error[0].value == (
        "Error: "
        + safe_markdown("Reasoning model not reachable at http://m:8000.")
        + "\n\nFix: "
        + safe_markdown("`herness deploy up reasoning`; `herness doctor`.")
    )
    at = AppTest.from_function(error_script, args=("query",)).run()
    assert at.error[0].value.startswith("**Block X**\n\nError: query x failed")
    shown = [t.value for t in at.expander[0].text]
    assert shown[:2] == ["Error type: QueryError", "run_id: run_7"]


def test_ut09_96_sidebar_build_lines() -> None:
    """UT09-96 sidebar_build: build line, data-as-of in UTC, D2 warning listing keys."""

    def script() -> None:
        from datetime import UTC, datetime  # noqa: PLC0415

        from app.common.widgets import sidebar_build  # noqa: PLC0415

        sidebar_build("20260101-000000-ABCDEF", datetime(2026, 1, 2, 3, 4, tzinfo=UTC), ["a.b"])

    at = AppTest.from_function(script).run()
    assert not at.exception
    assert [c.value for c in at.sidebar.caption] == [
        "Build `20260101-000000-ABCDEF`",
        "Data as of 2026-01-02 03:04 UTC",
    ]
    assert [w.value for w in at.sidebar.warning] == [
        "Dollar weights are placeholders and not yet confirmed: a.b"
    ]


def test_ut09_96_query_error_message_sanitised() -> None:
    """UT09-96 error text is shown through safe_markdown (no link or HTML survives)."""
    exc = QueryError("bad [x](http://e.example) <b>")
    what, _fix = widgets.user_message(exc)
    assert "](http" not in safe_markdown(what)
