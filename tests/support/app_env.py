"""Dashboard test helpers (impl 09 §11, T09-13): UI configs, fake services, AppTest scripts.

``fake_services`` stands in for ``app.common.bootstrap.get_services`` (only ``cfg.security.ui``
and ``roster`` are read by ``current_actor``); ``actor_script`` is an AppTest script that shows
the session actor as ``role|user_ref|display``. T09-14 adds ``wh_services`` (the fields the
warehouse and ops-cache modules read), ``wh_script`` and ``plant_scored_build``. T09-15 adds
the page list ``APP_PAGES``, ``page_services`` and ``install_session``. T09-16 appends
pages 01 - 04 and adds ``install_redaction_double``.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.common.auth import build_roster
from herness.core.settings import ExposeConfig, RolesConfig, UiConfig

USER_REF_KEY = b"app-unit-test-key-not-secret"  # pragma: allowlist secret
PROXY = "127.0.0.1"


@contextmanager
def preserved_main() -> Iterator[None]:
    """Restore ``sys.modules["__main__"]`` after AppTest runs.

    AppTest leaves its temporary script installed as ``__main__``; a later ``spawn`` child
    process would re-run that script (seen in test_jobs_gpu_lock).
    """
    saved = sys.modules.get("__main__")
    try:
        yield
    finally:
        if saved is not None:
            sys.modules["__main__"] = saved


def ui_config(
    *,
    exposed: bool = False,
    bind: str = "127.0.0.1",
    proxy: str | None = PROXY,
    admins: Sequence[str] = (),
    reviewers: Sequence[str] = (),
    default_role: str = "viewer",
) -> UiConfig:
    """A ``security.ui`` section; exposure through ``proxy`` when ``exposed``."""
    roles = RolesConfig.model_validate(
        {"admins": list(admins), "reviewers": list(reviewers), "default_role": default_role}
    )
    expose = ExposeConfig(enabled=exposed, trusted_proxy=proxy if exposed else None)
    return UiConfig(bind=bind, expose=expose, roles=roles)


def fake_services(ui: UiConfig) -> SimpleNamespace:
    """The two ``AppServices`` fields ``current_actor`` reads."""
    cfg = SimpleNamespace(security=SimpleNamespace(ui=ui))
    return SimpleNamespace(cfg=cfg, roster=build_roster(ui.roles, USER_REF_KEY))


def actor_script() -> None:
    """AppTest script: resolve the session actor and show it."""
    import streamlit as st  # noqa: PLC0415 - AppTest runs this function's body as a script

    from app.common.auth import current_actor  # noqa: PLC0415

    actor = current_actor()
    st.text(f"{actor.role}|{actor.user_ref}|{actor.display}")


# --- T09-14: warehouse access, named queries, ops caches -------------------------------------

QUERIES_FILE = Path(__file__).resolve().parents[2] / "app" / "common" / "queries.sql.yaml"


def wh_services(
    data_dir: Path,
    *,
    queries: Mapping[str, object] | None = None,
    recheck_s: int = 60,
    page_row_limit: int = 5000,
) -> SimpleNamespace:
    """The ``AppServices`` fields ``app.common.wh`` and ``app.common.data`` read."""
    ttl = SimpleNamespace(warehouse=3600, ops=10)
    app = SimpleNamespace(
        current_recheck_s=recheck_s, page_row_limit=page_row_limit, cache_ttl_s=ttl
    )
    cfg = SimpleNamespace(paths=SimpleNamespace(data=data_dir), app=SimpleNamespace(app=app))
    return SimpleNamespace(cfg=cfg, queries=dict(queries or {}))


def wh_script() -> None:
    """AppTest script: show the build id ``get_conn`` returns."""
    import streamlit as st  # noqa: PLC0415 - AppTest runs this function's body as a script

    from app.common.wh import get_conn  # noqa: PLC0415

    build_id, _conn = get_conn()
    st.text(build_id)


def plant_scored_build(warehouse_dir: Path) -> str:
    """Write the scored ``metrics_tiny`` build as ``wh-<id>.duckdb`` and point CURRENT at it.

    Spec 11's ``tiny_build`` is not in the tree: this reuses the impl 04 pattern
    (``metrics_tiny`` + real facts + real ``run_scoring`` on the small catalog, then
    ``save_as_file``), so every schema the named queries read (meta, core, enrich, metrics,
    score) exists with real column shapes. Returns the build id.
    """
    from tests.support.metrics_scoring import (  # noqa: PLC0415
        patches,
        save_as_file,
        small_catalog,
        tiny_with_facts,
    )
    from tests.support.metrics_tiny import BUILD_ID  # noqa: PLC0415

    from herness.metrics.scoring import run_scoring  # noqa: PLC0415
    from herness.store._warehouse_rw import write_current  # noqa: PLC0415
    from herness.store.layout import DataLayout  # noqa: PLC0415

    warehouse_dir.mkdir(parents=True, exist_ok=True)
    with pytest.MonkeyPatch.context() as mp:
        for obj, name, value in patches(small_catalog()):
            mp.setattr(obj, name, value)
        con = tiny_with_facts()
        try:
            run_scoring(BUILD_ID, con=con)
            save_as_file(con, warehouse_dir / f"wh-{BUILD_ID}.duckdb")
        finally:
            con.close()
    write_current(BUILD_ID, layout=DataLayout.from_root(warehouse_dir.parent))
    return BUILD_ID


# --- T09-15: page wrapper, widgets, page list ---------------------------------------------------

APP_DIR = Path(__file__).resolve().parents[2] / "app"
NO_BUILD_TEXT = "No promoted build yet. Run `herness pipeline`."


@dataclass(frozen=True)
class PageSpec:
    """One dashboard page script for the page-list tests (ST09-08, IT09-10, IT09-11).

    ``key_texts`` must appear on the page rendered over a scored build (IT09-10). T09-16 -
    T09-19 append their pages to ``APP_PAGES``.
    """

    path: Path
    needs_warehouse: bool
    key_texts: tuple[str, ...]


APP_PAGES: tuple[PageSpec, ...] = (
    PageSpec(
        APP_DIR / "Home.py",
        needs_warehouse=False,
        key_texts=("Builds", "Data quality", "Runs", "Jobs", "Workers", "Sources"),
    ),
    # T09-16: pages 01 - 04 (texts as rendered over the planted scored build)
    PageSpec(
        APP_DIR / "pages" / "01_Funding_Ranking.py",
        needs_warehouse=True,
        key_texts=("Funding Ranking", "Filters", "Ranking", "Detail", "Attribution"),
    ),
    PageSpec(
        APP_DIR / "pages" / "02_Portfolio.py",
        needs_warehouse=True,
        key_texts=(
            "Portfolio",
            "Scenario",
            "Table",
            "Chart",
            "Solver status: OPTIMAL",
            "No candidates selected in this scenario.",
        ),
    ),
    PageSpec(
        APP_DIR / "pages" / "03_Org_Scorecards.py",
        needs_warehouse=True,
        key_texts=("Org Scorecards", "Filters", "Peer table", "Trend"),
    ),
    PageSpec(
        APP_DIR / "pages" / "04_Action_Levers.py",
        needs_warehouse=True,
        key_texts=("Action Levers", "Filters", "Levers", "No action levers for this selection."),
    ),
)


def _weights(unconfirmed: Sequence[str]) -> SimpleNamespace:
    blocks = {key: {"unconfirmed": True} for key in unconfirmed}
    return SimpleNamespace(model_dump=lambda: dict(blocks))


def page_services(
    data_dir: Path,
    *,
    ui: UiConfig | None = None,
    unconfirmed: Sequence[str] = (),
    queries: Mapping[str, object] | None = None,
) -> SimpleNamespace:
    """The ``AppServices`` fields the page wrapper, auth, warehouse and ops cache read.

    ``queries`` defaults to the shipped ``queries.sql.yaml``.
    """
    from app.common.wh import load_named_queries  # noqa: PLC0415 - keeps the module import light

    if queries is None:
        queries = load_named_queries(QUERIES_FILE, page_row_limit=5000)
    ui = ui or ui_config(reviewers=["alice"])
    services = wh_services(data_dir, queries=queries)
    services.cfg.security = SimpleNamespace(ui=ui)
    services.cfg.weights = _weights(unconfirmed)
    services.roster = build_roster(ui.roles, USER_REF_KEY)
    services.cfg.app.reports = SimpleNamespace(top_n=10)  # `cfg.app.reports.top_n` (T09-16)
    return services


RUN_ID = "run_01J0000000000000000000000A"
FAILED_JOB_ID = "job_01J0000000000000000000000B"
HIDDEN_ERROR_TEXT = "SECRET ticket text in an error message"


def seed_home_ops() -> None:
    """One run, one failed job (``last_error`` with a message) and one open breaker."""
    import json  # noqa: PLC0415
    import sqlite3  # noqa: PLC0415
    from datetime import timedelta  # noqa: PLC0415

    from herness.core import time as clock  # noqa: PLC0415
    from herness.store.ops import core  # noqa: PLC0415

    now = clock.now()
    at = clock.format_utc(now - timedelta(hours=1))
    error = json.dumps({"class": "QueryError", "message": HIDDEN_ERROR_TEXT})

    def fn(conn: sqlite3.Connection) -> None:
        conn.execute(
            "INSERT INTO run (run_id, kind, depth, profile, config_hash, status, started_at,"
            " finished_at, token_usage, cost_usd) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (RUN_ID, "funding_review", "fast", "p", "c", "done", at, at, '{"input": 3}', "1.25"),
        )
        conn.execute(
            "INSERT INTO job (job_id, kind, gpu_class, status, priority, payload, last_error,"
            " attempts, max_attempts, scheduled_for, created_at, finished_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (FAILED_JOB_ID, "review", "none", "failed", 50, "{}", error, 3, 3, at, at, at),
        )
        conn.execute(
            "INSERT INTO source_health (source, state, updated_at) VALUES (?,?,?)",
            ("servicenow", "open", at),
        )

    core.run_write(fn, op="test_seed")


def install_session(
    mp: pytest.MonkeyPatch, services: SimpleNamespace, *, user: str = "alice"
) -> list[dict[str, object]]:
    """Patch ``get_services``, the OS user, the user-ref key and ``audit``; return audit calls."""
    from app.common import auth, bootstrap  # noqa: PLC0415 - keeps the module import light

    calls: list[dict[str, object]] = []
    mp.setattr(bootstrap, "get_services", lambda: services)
    mp.setattr(auth, "_process_user", lambda **_: user)
    mp.setattr(auth, "load_user_ref_key", lambda: USER_REF_KEY)
    mp.setattr(auth, "audit", lambda event, actor, **f: calls.append({"event": event, **f}))
    return calls


# --- T09-16: redaction test double -----------------------------------------------------------

REDACTED_TITLE = "redacted title #"


def install_redaction_double(mp: pytest.MonkeyPatch) -> list[str]:
    """Replace ``herness.core.redact.redact_text`` (tests load no redaction key).

    The double returns ``redacted title #<n>`` (never the input) and records every input, so a
    test can assert a title went through redaction and its raw value is not on screen.
    """
    from herness.core import redact  # noqa: PLC0415 - keeps the module import light

    seen: list[str] = []

    def fake(text: str | None) -> str | None:
        if text is None:
            return None
        seen.append(text)
        return f"{REDACTED_TITLE}{len(seen)}"

    mp.setattr(redact, "redact_text", fake)
    return seen
