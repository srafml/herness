"""Security tests of the ServiceNow connector (impl 01 ST01-02, ST01-03, ST01-05; TH01-02,
TH01-03, TH01-05; T01-16; carry-overs of T01-14 and T01-15).

ST01-02 and ST01-03 run the production-built connector (egress source client with its host
guard, ``build_auth`` OAuth password grant with ``synthetic`` sentinel secrets) inside the
real ``SyncRunner`` over the migrated ops store and the real ``LakeWriter``; only the pool
transport is a mock (``_servicenow_env``). ST01-05 runs the connector over a mock-transport
``SourceHttp`` inside the runner with the recording fake lake.
"""

from __future__ import annotations

import datetime
import json
import traceback
from collections.abc import Iterator
from pathlib import Path

import httpx2
import pytest
from structlog.testing import capture_logs
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.support.sn_cassettes import FOREIGN_HOST, HOST, FakeServiceNow, pair
from tests.unit.connectors import _servicenow_env as env
from tests.unit.connectors._backfill_data import slices
from tests.unit.connectors._http_data import bind_resilience, mock_client
from tests.unit.connectors._mongo_data import SpyBreaker
from tests.unit.connectors._runner_data import NOW, T, make_runner

import herness.core.resilience.retry as retry_module
from herness.connectors.http import ForeignHostError, SourceHttp
from herness.connectors.servicenow import ServiceNowConnector
from herness.connectors.settings import ServiceNowSettings
from herness.core import config as c
from herness.core.errors import AuthError, HernessError, SchemaViolation, SourceUnavailable
from herness.core.resilience import ProcessState
from herness.core.resilience._state import process_state, require_ops_backend
from herness.store.ops import get_watermark, set_watermark

pytestmark = pytest.mark.unit

_SRC, _ENT = "servicenow", "incident"
_INCIDENT = "/api/now/table/incident"
_AUDIT = "/api/now/table/sys_audit_delete"


def _slice_errors_then_close_breaker() -> list[str]:
    """The `last_error` of each failed slice; the 401 force-opens the source breaker (08 §9.2),
    so close it again for the next phase."""
    errors = [str(r["last_error"]) for r in slices(_SRC) if r["last_error"]]
    require_ops_backend().health_reset([_SRC], datetime.datetime.now(datetime.UTC))
    process_state().breakers.pop(_SRC, None)
    return errors


@pytest.fixture
def waits(reset_process_state: ProcessState) -> list[float]:
    """Retry sleeps of `retry_page` recorded instead of waited."""
    slept: list[float] = []
    reset_process_state.sleep = slept.append
    return slept


@pytest.fixture
def sn_env(
    fake_keyring: MemoryKeyring, ops_store: OpsStoreHandle, waits: list[float], tmp_path: Path
) -> Iterator[ServiceNowSettings]:
    """Loaded config (ServiceNow on the synthetic host), sentinel OAuth secret, ops store
    bound as the resilience backend; yields the section."""
    del fake_keyring, waits
    env.store_secret()
    settings = env.load(tmp_path)
    bind_resilience()
    assert ops_store.data_root == tmp_path / "data"
    yield settings
    c.reset_config()


def _error_text(err: BaseException) -> str:
    """Everything an error can show: message, repr, fields and the whole chained traceback."""
    parts = [str(err), repr(err), str(err.__dict__)]
    parts += [str(getattr(err, name, "")) for name in ("context", "details", "hint")]
    parts += traceback.format_exception(err)
    return "\n".join(parts)


def _assert_clean(text: str | bytes) -> None:
    for sentinel in env.SENTINELS:
        needle = sentinel.encode() if isinstance(text, bytes) else sentinel
        assert needle not in text, sentinel


def _written_bytes(root: Path) -> bytes:
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


# --- ST01-03: no secret in logs, errors, results, lake or sync_slice --------------------------


def test_st01_03_servicenow_sync_failing_with_401_and_500_leaks_no_secret(
    sn_env: ServiceNowSettings,
    ops_store: OpsStoreHandle,
    waits: list[float],
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST01-03 ServiceNow end to end with sentinel secrets: a sync succeeds, then fails with
    401 (token refetched, request retried, then AuthError), 500 (SourceUnavailable after
    retries) and a refused token endpoint, all through servicenow.py: no sentinel in the
    captured logs (DEBUG included), exception strings and tracebacks, the SyncResults, the
    lake files, sync_slice.last_error or any file written under the data root."""
    fake = FakeServiceNow.from_cassette("incident_table")
    host = env.SnHost(fake)
    env.install(monkeypatch, host)
    clock = env.Clock()
    runner = env.runner(sn_env, clock, ops_store.data_root)
    window = (clock.now - datetime.timedelta(days=1), clock.now)
    errors: list[HernessError] = []
    last_errors: list[str] = []

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)  # no watermark yet: backfill from 2026-02-20
        fake.fail[_INCIDENT] = 401
        with pytest.raises(AuthError) as refused:
            runner.run_backfill(_ENT, *window)
        errors.append(refused.value)
        last_errors += _slice_errors_then_close_breaker()
        fake.fail[_INCIDENT] = 500
        with pytest.raises(SourceUnavailable) as down:
            runner.run_backfill(_ENT, *window)
        errors.append(down.value)
        last_errors += _slice_errors_then_close_breaker()
        host.token = [401]
        fake.fail[_INCIDENT] = 401
        clock.now += datetime.timedelta(hours=1)
        with pytest.raises(AuthError) as token_refused:
            runner.run_incremental(_ENT)
        errors.append(token_refused.value)

    # The sentinels were really in play: the form carried them, the data requests the token.
    assert host.form()["client_secret"] == env.CLIENT_SECRET
    assert host.form()["password"] == env.PASSWORD
    assert f"Bearer {env.ACCESS}1" in host.bearers
    assert f"Bearer {env.ACCESS}2" in host.bearers  # the 401 refetched the token
    assert "HTTP 401 GET /api/now/table/incident" in str(refused.value)
    assert "HTTP 500 GET /api/now/table/incident" in str(down.value)
    assert "HTTP 401 POST /oauth_token.do" in str(token_refused.value)
    assert waits  # the 500 was retried before it surfaced
    assert result.rows == 244  # 240 records and 4 tombstones
    assert result.tombstones == 4
    assert [e.split(":")[0] for e in last_errors] == ["AuthError", "SourceUnavailable"]
    assert any(e["log_level"] == "debug" for e in logs)
    lake = env.lake_rows(ops_store.data_root)
    assert len(lake) == 244

    texts = [repr(logs), *(_error_text(err) for err in errors), repr(result), repr(lake)]
    texts += [json.dumps(result.to_dict(data_root=ops_store.data_root)), "\n".join(last_errors)]
    for text in texts:
        _assert_clean(text)
    written = _written_bytes(ops_store.data_root)
    assert b"SourceUnavailable" in written  # the ops store was scanned
    _assert_clean(written)
    _assert_clean(_written_bytes(tmp_path))


# --- ST01-02: a foreign next link never receives a request -----------------------------------


def test_st01_02_servicenow_foreign_next_link_raises_without_a_request(
    sn_env: ServiceNowSettings, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ST01-02 a `Link rel=next` from ServiceNow naming another host raises ForeignHostError
    inside the sync; no request reaches the other host (the bearer token never leaves the
    source host); nothing is committed and no watermark is set."""
    fake = FakeServiceNow.from_cassette("incident_table", link_next=True, next_host=FOREIGN_HOST)
    host = env.SnHost(fake)
    env.install(monkeypatch, host)
    runner = env.runner(sn_env, env.Clock(), ops_store.data_root)

    with pytest.raises(ForeignHostError) as caught:
        runner.run_incremental(_ENT)

    assert caught.value.host == FOREIGN_HOST
    assert {r.url.host for r in fake.requests} == {HOST}
    assert len(fake.table_requests("incident")) == 1  # the full first page carried the link
    assert all(b == f"Bearer {env.ACCESS}1" for b in host.bearers)
    assert env.lake_files(ops_store.data_root) == []
    assert get_watermark(_SRC, _ENT) is None


def test_st01_02_servicenow_check_next_url_guards_the_connector_itself(
    monkeypatch: pytest.MonkeyPatch, reset_process_state: ProcessState
) -> None:
    """ST01-02 the connector's own `check_next_url` call stops the foreign link even over a
    client without the egress host guard (mock transport)."""
    del reset_process_state
    spy = SpyBreaker()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors.http.breaker", lambda _key: spy)
    fake = FakeServiceNow.from_cassette("incident_table", link_next=True, next_host=FOREIGN_HOST)
    http = SourceHttp(mock_client(fake, base_url=env.BASE_URL), breaker_key=_SRC, auth=None)
    cfg = ServiceNowSettings.model_validate(
        {
            "enabled": True,
            "base_url": env.BASE_URL,
            "auth": {"method": "basic", "credentials": "secret:sn"},
            "page_size": 100,
            "entities": {"incident": {"fields": ["number"], "window_hours": 48}},
        }
    )
    conn = ServiceNowConnector(cfg, http=http, clock=lambda: env.NOW)
    since = env.NOW - datetime.timedelta(days=3)
    with pytest.raises(ForeignHostError):
        list(conn.sync(_ENT, since, env.NOW))
    assert {r.url.host for r in fake.requests} == {HOST}


# --- ST01-05: malformed ServiceNow rows keep the watermark and commit nothing -----------------


def _servicenow_cfg() -> ServiceNowSettings:
    cfg = ServiceNowSettings.model_validate(
        {
            "enabled": True,
            "base_url": env.BASE_URL,
            "auth": {"method": "basic", "credentials": "secret:sn"},
            "page_size": 100,
            "entities": {"incident": {"fields": ["number"]}},
        }
    )
    return cfg.model_copy(update={"batch_rows": 2})  # a batch is written before the bad row


def _good(i: int) -> dict[str, object]:
    at = (T + datetime.timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S")
    return {"sys_id": pair(f"{i:032x}"), "sys_updated_on": pair(at), "number": pair(f"INC{i}")}


@pytest.mark.parametrize(
    "body",
    [
        {"result": {"sys_id": pair("a"), "sys_updated_on": pair("2026-03-01 10:30:00")}},
        {"result": [_good(1), _good(2), _good(3), {"sys_id": pair("b"), "number": pair("X")}]},
    ],
    ids=["result-object", "no-sys-updated-on"],
)
def test_st01_05_servicenow_rows_keep_watermark_and_commit_nothing(
    body: dict[str, object],
    ops_store: OpsStoreHandle,
    lake: FakeLake,
    reset_process_state: ProcessState,
    guard: object,
) -> None:
    """ST01-05 through servicenow.py: `result` as an object, or a record without
    `sys_updated_on` after a written batch of good ones: SchemaViolation; the open writer
    is aborted, nothing is committed and the watermark is unchanged."""
    del reset_process_state, guard
    bind_resilience()
    set_watermark(_SRC, _ENT, "sys_updated_on", T, now=NOW)

    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path == _AUDIT:
            return httpx2.Response(403, json={})
        return httpx2.Response(200, json=body)

    http = SourceHttp(mock_client(handler, base_url=env.BASE_URL), breaker_key=_SRC, auth=None)
    conn = ServiceNowConnector(_servicenow_cfg(), http=http, clock=lambda: NOW)
    runner = make_runner(conn, _servicenow_cfg(), lake, ops_store.data_root)

    with pytest.raises(SchemaViolation) as caught:
        runner.run_incremental(_ENT)

    wm = get_watermark(_SRC, _ENT)
    assert wm is not None
    assert wm.value == T
    assert "commit" not in lake.kinds()
    if isinstance(body["result"], list):
        assert lake.kinds().count("write") == 1  # the good batch was written, then aborted
        assert "abort" in lake.kinds()
        assert "unparseable timestamp in sys_updated_on" in str(caught.value)
        assert "INC" not in _error_text(caught.value)
    else:
        assert "result is not a list" in str(caught.value)
