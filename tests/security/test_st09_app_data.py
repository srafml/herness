"""ST09-12, ST09-13, ST09-17, ST09-25: named-query guard, injection, trace paths, chat caching."""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import streamlit as st
from sqlglot import exp, parse_one
from sqlglot.optimizer.scope import traverse_scope
from streamlit.testing.v1 import AppTest
from tests.support.app_env import QUERIES_FILE, plant_scored_build, preserved_main, wh_services
from tests.support.ops_store import OpsStoreHandle

from app.common import bootstrap, data, wh
from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.reports.rules import UserInputError
from herness.store.layout import DataLayout
from herness.store.ops import chat
from herness.store.warehouse import open_readonly

pytestmark = pytest.mark.unit

ALLOWED_CORE = frozenset(
    {
        "record_id", "number", "opened_at", "priority", "project", "type", "team_id", "org_id",
        "service_id", "name", "parent_org_id",
    }
)  # fmt: skip
INJECTIONS = ("' OR 1=1 --", "x'); DROP TABLE core.incident; --")
RUN_ID = "run_" + "1" * 26


@pytest.fixture(autouse=True)
def _isolated() -> Iterator[None]:
    st.cache_data.clear()
    with preserved_main():
        yield
    st.cache_data.clear()


# --- ST09-12 raw-text guard over the shipped file ----------------------------------------------


def _core_columns(sql: str) -> set[str]:
    """Every column a query reads from a `core` table (aliases resolved per scope)."""
    found: set[str] = set()
    for scope in traverse_scope(parse_one(sql, read="duckdb")):
        core = {
            alias: src.name
            for alias, src in scope.sources.items()
            if isinstance(src, exp.Table) and src.db == "core"
        }
        for col in scope.columns:
            if col.table in core or (not col.table and core):
                found.add(col.name)
    return found


def test_st09_12_shipped_queries_pass_guard() -> None:
    """ST09-12 parse queries.sql.yaml: no forbidden column, no `core` star, allowed core only."""
    queries = wh.load_named_queries(QUERIES_FILE, page_row_limit=5000)
    assert wh.check_forbidden_columns(queries) == []
    read = set().union(*(_core_columns(q.sql) for q in queries.values()))
    assert read <= ALLOWED_CORE
    assert "text" not in read  # redacted text only from enrich.text_redacted


def test_st09_12_guard_trips_startup(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """ST09-12 a raw-text query in the file stops the dashboard start-up (U09-53 step 10)."""
    path = tmp_path / "q.yaml"
    path.write_text(
        "version: 1\nqueries:\n  a.raw: {sql: 'SELECT short_description FROM core.incident'}\n",
        encoding="utf-8",
    )
    with pytest.raises(ConfigError, match=r"^named query a\.raw reads raw ticket text$"):
        bootstrap.load_queries(path, page_row_limit=5000)


# --- ST09-13 injection through filter values ---------------------------------------------------


@pytest.fixture(scope="module")
def scored(tmp_path_factory: pytest.TempPathFactory) -> Path:
    data_root = tmp_path_factory.mktemp("st09_13") / "data"
    plant_scored_build(data_root / "warehouse")
    return data_root


@pytest.mark.parametrize("value", INJECTIONS)
@pytest.mark.parametrize(
    ("name", "extra"),
    [
        ("funding.ranking_by_priority", {}),
        ("funding.attribution", {}),
        ("org.scores", {"entity_type": "team", "metric": "mtta_minutes"}),
        ("clusters.list", {}),
        ("metrics.values", {"metrics": ["incident_count"], "period": "month"}),
        ("evidence.meta", {}),
        ("review.label_text", {}),
    ],
)
def test_st09_13_filter_values_are_literals(
    monkeypatch: pytest.MonkeyPatch, scored: Path, name: str, extra: dict[str, Any], value: str
) -> None:
    """ST09-13 `' OR 1=1 --` and `x'); DROP TABLE` bound as literals: empty result, no error."""
    queries = wh.load_named_queries(QUERIES_FILE, page_row_limit=5000)
    services = wh_services(scored, queries=queries)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    build = wh.current_build_id(scored)
    conn = open_readonly(build, layout=DataLayout.from_root(scored))
    monkeypatch.setattr(wh, "get_conn", lambda: (build, conn))
    nq = queries[name]
    params: dict[str, Any] = dict(extra)
    for pname, spec in nq.params.items():
        if pname not in params and spec.type == "str":
            params[pname] = value
        elif pname not in params and spec.type == "list_str":
            params[pname] = [value]
    result = wh.query(name, **params)
    assert result.table.num_rows == 0
    row = conn.execute("SELECT count(*) FROM core.incident").fetchone()
    assert row is not None
    assert row[0] > 0  # nothing dropped


# --- ST09-17 trace path containment ------------------------------------------------------------


@pytest.fixture
def traces(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    services = wh_services(tmp_path / "data")
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    root = tmp_path / "data" / "traces"
    root.mkdir(parents=True)
    return root


@pytest.mark.parametrize("run_id", ["../../x", "..\\..\\x", "run_/../../x", "/etc/passwd"])
def test_st09_17_traversal_refused_before_file_access(
    traces: Path, monkeypatch: pytest.MonkeyPatch, run_id: str
) -> None:
    """ST09-17 a `run_id` with path parts is refused before any file access."""
    del traces

    def no_access(*_a: object, **_k: object) -> None:
        msg = "file accessed"
        raise AssertionError(msg)

    monkeypatch.setattr(Path, "open", no_access)
    monkeypatch.setattr(Path, "resolve", no_access)
    monkeypatch.setattr(Path, "is_symlink", no_access)
    with pytest.raises(UserInputError):
        data.read_trace_page(run_id, type_filter=None, page=0, include_payload=True)


def test_st09_17_symlink_out_of_root_refused(
    traces: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-17 a trace file that is a symlink out of the root is refused, never read."""
    outside = tmp_path / "secret.jsonl"
    outside.write_text('{"type": "x", "secret": 1}\n', encoding="utf-8")
    link = traces / f"{RUN_ID}.jsonl"
    try:
        os.symlink(outside, link)
    except OSError:  # no symlink privilege: drive the same branch through is_symlink
        link.write_text('{"type": "x", "secret": 1}\n', encoding="utf-8")
        monkeypatch.setattr(Path, "is_symlink", lambda self: self.name == link.name)
    opened: list[str] = []
    real_open = Path.open

    def spy(self: Path, *a: Any, **k: Any) -> Any:
        opened.append(self.name)
        return real_open(self, *a, **k)

    monkeypatch.setattr(Path, "open", spy)
    with pytest.raises(UserInputError):
        data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=True)
    assert opened == []


def test_st09_17_traces_dir_symlink_escape_refused(
    traces: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST09-17 a trace path resolving outside the traces directory is refused."""
    real_resolve = Path.resolve

    def escaping(self: Path, *a: Any, **k: Any) -> Path:
        if self.name == f"{RUN_ID}.jsonl":
            return tmp_path / "elsewhere" / self.name
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", escaping)
    (traces / f"{RUN_ID}.jsonl").write_text('{"type": "x"}\n', encoding="utf-8")
    with pytest.raises(UserInputError):
        data.read_trace_page(RUN_ID, type_filter=None, page=0, include_payload=True)


# --- ST09-25 chat never cached across users ----------------------------------------------------


def chat_script() -> None:
    """AppTest script: list this session's chat sessions (uncached) and probe ops_read."""
    import streamlit as st  # noqa: PLC0415 - AppTest runs this function's body as a script

    from app.common.data import ops_read  # noqa: PLC0415
    from herness.core.errors import ConfigError  # noqa: PLC0415
    from herness.store.ops.chat import list_chat_sessions  # noqa: PLC0415

    user_ref = st.session_state["user_ref"]
    rows = list_chat_sessions(user_ref)
    st.text(",".join(sorted(r["session_id"] for r in rows)))
    try:
        ops_read("list_chat_sessions", user_ref=user_ref)
    except ConfigError:
        st.text("refused")


def test_st09_25_two_users_disjoint_no_cached_chat(
    monkeypatch: pytest.MonkeyPatch, ops_store: OpsStoreHandle
) -> None:
    """ST09-25 two users in parallel sessions: disjoint lists; chat never in an ops cache."""
    services = wh_services(ops_store.data_root)
    monkeypatch.setattr(bootstrap, "get_services", lambda: services)
    now = clock.now()
    alice = {chat.create_chat_session("a" * 32, now=now) for _ in range(2)}
    bob = {chat.create_chat_session("b" * 32, now=now + datetime.timedelta(seconds=1))}
    seen: dict[str, AppTest] = {}
    for ref in ("a" * 32, "b" * 32):
        at = AppTest.from_function(chat_script)
        at.session_state["user_ref"] = ref
        seen[ref] = at.run()
    seen["a" * 32].run()  # interleaved reruns keep each session's own list
    lists = {ref: set(at.text[0].value.split(",")) for ref, at in seen.items()}
    assert lists == {"a" * 32: alice, "b" * 32: bob}
    assert all(at.text[1].value == "refused" for at in seen.values())
    later = chat.create_chat_session("a" * 32, now=now + datetime.timedelta(seconds=2))
    seen["a" * 32].run()  # not cached: the new session shows at once
    assert set(seen["a" * 32].text[0].value.split(",")) == alice | {later}
    assert not any(name.startswith(("chat", "list_chat", "get_chat")) for name in data.OPS_READS)
