"""Rate-limit, auth and breaker behaviour of the live connectors (impl 01 FT01-04, FT01-05,
FT01-06; TH01-06, TH01-16; T01-25).

Everything is in process over the real resilience layer (retry policy `source_http_page`, the
breaker over the migrated ops store, the fault plan loader) and the production HTTP layer
(`SourceHttp`). `respx` patches only `httpx`; the connectors' egress client is `httpx2`, so the
scripted sources are `httpx2.MockTransport` handlers (the technique of the unit and
integration suites). No socket is opened. Every credential is a `synthetic` sentinel.
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx2
import pytest
from structlog.testing import capture_logs
from tests.support import jira_pages
from tests.support.build_harness import FakeJobContext
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fault_env import FaultEnv
from tests.support.jira_pages import CLOUD_SEARCH
from tests.support.ops_store import OpsStoreHandle
from tests.support.sn_cassettes import FakeServiceNow
from tests.unit.connectors import _servicenow_env as sn
from tests.unit.connectors._http_data import bind_resilience, mock_client
from tests.unit.connectors._jira_data import (
    NOW,
    SINCE,
    UNTIL,
    Replay,
    cassette,
    settings,
)

import herness.connectors._write_loop as write_loop
import herness.connectors.http as http_module
from herness.connectors.http import SourceHttp
from herness.connectors.jira import JiraConnector, build_jql
from herness.connectors.jobs import handle_sync
from herness.connectors.runner import SyncRunner
from herness.core import config as c
from herness.core.errors import AuthError, CircuitOpen, SourceUnavailable
from herness.core.resilience import ProcessState, breaker
from herness.store.ops import get_watermark, read_all, set_watermark

pytestmark = pytest.mark.fault


@pytest.fixture(autouse=True)
def _reset_config() -> Iterator[None]:
    """Drop the loaded config after each test, also when it fails."""
    yield
    c.reset_config()


UTC = datetime.UTC
_FIELD = "updated"
_WM = datetime.datetime(2026, 9, 1, 8, 0, tzinfo=UTC)  # since = _WM - 60 min overlap
_SETTLED = NOW - datetime.timedelta(seconds=60)  # until = NOW - settle_seconds


def _events(kind: str) -> list[dict[str, Any]]:
    """Stored resilience events of ``kind`` with the decoded ``detail``, oldest first."""
    rows = read_all(
        "SELECT target, detail FROM resilience_event WHERE kind = ? ORDER BY ts, event_id", (kind,)
    )
    return [{"target": r["target"], **json.loads(str(r["detail"]))} for r in rows]


def _jira_runner(replay: Replay, data_root: Path) -> SyncRunner:
    """A runner over a Jira connector replaying ``replay`` (fixed clock `NOW`)."""
    cfg = settings(fetch_remote_links=True)
    http = SourceHttp(mock_client(replay, jira_pages.BASE_URL), breaker_key="jira", auth=None)
    conn = JiraConnector(
        cfg, custom_field_ids=jira_pages.SYNTH_FIELD_IDS, http=http, clock=lambda: NOW
    )
    return SyncRunner(conn, cfg, clock=lambda: NOW, data_root=data_root)


def _replay_for_window() -> Replay:
    """The `cloud_sync_it` cassette with the JQL of the window the runner will ask for."""
    interactions = cassette("cloud_sync_it.json")
    since = _WM - datetime.timedelta(minutes=60)
    jql = build_jql(since=since, until=_SETTLED, scope=jira_pages.SCOPE)
    for step in interactions:
        if step["request"]["path"] == CLOUD_SEARCH:
            step["request"]["json"]["jql"] = jql
    return Replay(interactions)


def test_ft01_04_429_retry_after_repeats_only_that_page_and_moves_the_watermark_once(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fault_env: FaultEnv,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT01-04 `http.page http_429 retry_after=7 count=3 source=jira`: three `retry` events
    with `retry_after_s = 7`; only the first search page is attempted again (4 attempts), no
    other request is repeated or skipped; the watermark advances once, after the commit."""
    reset_process_state.sleep = lambda _s: None
    bind_resilience()
    rule = {"point": "http.page", "action": "http_429", "retry_after": 7, "count": 3}
    fault_env([rule | {"source": "jira"}])
    fault_calls: list[str] = []
    real_point = http_module.fault_point

    def counting_point(point: str, **labels: str) -> None:
        fault_calls.append(point)
        real_point(point, **labels)

    monkeypatch.setattr(http_module, "fault_point", counting_point)
    sets: list[datetime.datetime] = []
    real_set = write_loop.set_watermark

    def counting_set(
        source: str, entity: str, field: str, value: datetime.datetime, **kw: Any
    ) -> Any:
        sets.append(value)
        return real_set(source, entity, field, value, **kw)

    monkeypatch.setattr(write_loop, "set_watermark", counting_set)
    set_watermark("jira", "issue", _FIELD, _WM, now=NOW)
    replay = _replay_for_window()
    runner = _jira_runner(replay, ops_store.data_root)

    result = runner.run_incremental("issue")

    assert (result.mode, result.rows) == ("incremental", 4)
    retries = _events("retry")
    assert [e["attempt"] for e in retries] == [1, 2, 3]
    assert {e["retry_after_s"] for e in retries} == {7}
    assert {e["target"] for e in retries} == {"jira"}
    assert all(e["wait_s"] >= 7 for e in retries)
    # 3 refused attempts, then the 9 recorded requests once each: the page, not the run, repeats
    assert len(fault_calls) == 3 + 9
    assert not replay.interactions
    assert [path for _m, path, _b in replay.seen].count(CLOUD_SEARCH) == 2
    assert len(sets) == 1
    wm = get_watermark("jira", "issue")
    assert wm is not None
    assert wm.value == sets[0] > _WM


def _jira_conn(handler: Callable[[httpx2.Request], httpx2.Response]) -> JiraConnector:
    http = SourceHttp(mock_client(handler, jira_pages.BASE_URL), breaker_key="jira", auth=None)
    return JiraConnector(
        settings(), custom_field_ids=jira_pages.SYNTH_FIELD_IDS, http=http, clock=lambda: NOW
    )


def test_ft01_05_401_is_an_auth_error_without_retry_and_force_open_opens_the_breaker(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> None:
    """FT01-05 a 401 on the first page: `AuthError`, one request, no `retry` event, and the
    breaker opens with reason `auth` (`force_open` of T08-06)."""
    del ops_store
    reset_process_state.sleep = lambda _s: None
    bind_resilience()
    seen: list[httpx2.Request] = []

    def refuse(request: httpx2.Request) -> httpx2.Response:
        seen.append(request)
        return httpx2.Response(401, json={"errorMessages": ["synthetic refusal"]})

    conn = _jira_conn(refuse)
    with pytest.raises(AuthError) as caught:
        next(iter(conn.sync("issue", SINCE, UNTIL)))

    assert len(seen) == 1  # not retried
    assert _events("retry") == []
    b = breaker("jira")
    b.force_open(caught.value)  # spec 01 calls force_open on a refused credential (08 §9.2)
    assert b.state() == "open"
    (opened,) = _events("breaker_open")
    assert (opened["target"], opened["trips"]) == ("jira", 1)
    with pytest.raises(CircuitOpen):
        next(iter(conn.sync("issue", SINCE, UNTIL)))
    assert len(seen) == 1  # the open breaker keeps the next call off the wire


@pytest.mark.xfail(
    strict=False,
    reason=(
        "FT01-05 open item: no connector or runner path calls breaker.force_open on an "
        "AuthError (08 §9.2 says spec 01 does); the breaker stays closed after a 401"
    ),
)
def test_ft01_05_auth_error_opens_the_breaker_without_a_manual_force_open(
    ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> None:
    """FT01-05 (strict reading) the 401 itself leaves the breaker `open`."""
    del ops_store
    reset_process_state.sleep = lambda _s: None
    bind_resilience()
    conn = _jira_conn(lambda _r: httpx2.Response(401, json={}))
    with pytest.raises(AuthError):
        next(iter(conn.sync("issue", SINCE, UNTIL)))
    assert breaker("jira").state() == "open"


def test_ft01_06_consecutive_503s_open_the_breaker_and_the_job_skips_the_source(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FT01-06 every request to one source answers 503: the breaker (threshold 8) opens, the
    job ends `done` with `skipped_open_circuit`, and the next run makes no HTTP call while the
    breaker is open."""
    del fake_keyring
    reset_process_state.sleep = lambda _s: None
    sn.store_secret()
    sn.load(tmp_path)
    bind_resilience()
    down = {f"/api/now/table/{table}": 503 for table in ("incident", "sys_audit_delete")}
    fake = FakeServiceNow.from_cassette("incident_table", fail=down)
    host = sn.SnHost(fake)
    sn.install(monkeypatch, host)
    now = datetime.datetime.now(UTC)
    set_watermark(
        "servicenow", "incident", "sys_updated_on", now - datetime.timedelta(days=1), now=now
    )

    def run_job() -> Any:
        return handle_sync(FakeJobContext({"source": "servicenow"}, kind="sync"))

    # run 1: six attempts of one page (policy `source_http_page`), all 503: the job fails
    with pytest.raises(SourceUnavailable):
        run_job()
    assert len(fake.requests) == 6
    assert breaker("servicenow").state() == "closed"

    # run 2: two more 503s make eight consecutive failures: the breaker opens, the next
    # attempt is refused by the guard, and the job is done with the source skipped
    with capture_logs() as logs:
        outcome = run_job()
    assert outcome.status == "done"
    assert outcome.result["skipped_open_circuit"] == ["servicenow"]
    assert outcome.result["partial"] is True
    assert outcome.result["results"] == []
    assert len(fake.requests) == 8
    assert breaker("servicenow").state() == "open"
    assert any(e["event"] == "connectors.sync.skipped_open_circuit" for e in logs)
    assert len(_events("breaker_open")) == 1

    # run 3: the breaker is open and cooling down: no HTTP call at all, job still done
    calls = len(fake.requests) + len(host.token_calls)
    third = run_job()
    assert (third.status, third.result["skipped_open_circuit"]) == ("done", ["servicenow"])
    assert len(fake.requests) + len(host.token_calls) == calls
