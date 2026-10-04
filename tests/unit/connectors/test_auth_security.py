"""Security tests of herness.connectors.auth (impl 01 ST01-03, ST01-15; TH01-03, TH01-16; T01-15).

ST01-03 runs real syncs: no ServiceNow connector exists before T01-16, so a test-local minimal
ServiceNow-shaped connector reads pages through the real ``SourceHttp`` page fetch with the
auth object from ``build_auth`` (OAuth password grant, every credential a ``synthetic``
sentinel, R-67), inside the real ``SyncRunner`` (incremental and backfill) over the temp ops
store and the recording fake lake. The ServiceNow-specific paths carry over to T01-16.
"""

from __future__ import annotations

import datetime
import json
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pyarrow as pa
import pytest
from pydantic import SecretStr
from structlog.testing import capture_logs
from tests.support.fake_clock import FIXTURE_START, FakeClock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors import _auth_data as d
from tests.unit.connectors._backfill_data import slices
from tests.unit.connectors._http_data import bind_resilience, mock_client
from tests.unit.connectors._runner_data import NOW, T, batch, make_runner, servicenow_cfg

from herness.connectors import auth as auth_module
from herness.connectors.auth import MsalTokenProvider, OAuthTokenAuth, build_auth
from herness.connectors.http import SourceHttp
from herness.connectors.settings_base import AuthSettings
from herness.core import config as c
from herness.core import time as clock
from herness.core.errors import AuthError, HernessError, SourceUnavailable
from herness.core.resilience import ProcessState
from herness.core.resilience._state import process_state, require_ops_backend
from herness.store.ops import set_watermark

pytestmark = [pytest.mark.unit, pytest.mark.usefixtures("guard")]

_SRC, _ENT = "servicenow", "incident"
_RECORD = {"sys_id": "a1", "sys_updated_on": "2026-03-01T10:30:00Z"}
_OAUTH_SECRET = {
    "client_id": d.CLIENT_ID,
    "client_secret": d.CLIENT_SECRET,
    "username": d.USERNAME,
    "password": d.PASSWORD,
}


@dataclass
class AuthedConnector:
    """Minimal ServiceNow-shaped connector: one page of `result` records via SourceHttp."""

    http: SourceHttp
    name: str = _SRC
    entities: tuple[str, ...] = (_ENT,)

    def check(self) -> None:
        return None

    def watermark_field(self, entity: str) -> str:
        return "sys_updated_on"

    def sync(
        self, entity: str, since: datetime.datetime | None, until: datetime.datetime | None = None
    ) -> Iterator[pa.RecordBatch]:
        body = self.http.get_json(d.DATA_PATH).body
        assert isinstance(body, dict)
        for record in body["result"]:
            ts = clock.parse_iso(str(record["sys_updated_on"]))
            yield batch(_SRC, entity, [str(record["sys_id"])], ts)


def _error_text(err: BaseException) -> str:
    """Everything an error can show: message, repr, fields and the whole chained traceback."""
    parts = [str(err), repr(err), str(err.__dict__)]
    parts += [str(getattr(err, name, "")) for name in ("context", "details", "hint")]
    parts += traceback.format_exception(err)
    return "\n".join(parts)


def _written_bytes(root: Path) -> bytes:
    """The bytes of every file under ``root`` (ops store database, WAL, lake, logs)."""
    return b"".join(p.read_bytes() for p in sorted(root.rglob("*")) if p.is_file())


def _assert_clean(text: str | bytes) -> None:
    for sentinel in d.SENTINELS:
        needle = sentinel.encode() if isinstance(text, bytes) else sentinel
        assert needle not in text, sentinel


@pytest.fixture
def no_sleep(reset_process_state: ProcessState) -> list[float]:
    """Retry sleeps of `retry_page` recorded instead of waited."""
    waits: list[float] = []
    reset_process_state.sleep = waits.append
    return waits


@pytest.fixture(autouse=True)
def _isolate(
    fake_keyring: MemoryKeyring, ops_store: OpsStoreHandle, reset_process_state: ProcessState
) -> Iterator[None]:
    bind_resilience()
    yield
    c.reset_config()


def _slice_errors_then_close_breaker() -> list[str]:
    """The `last_error` of each slice; the 401 force-opens the source breaker (08 §9.2), so
    close it again for the next phase."""
    errors = [str(r["last_error"]) for r in slices(_SRC)]
    require_ops_backend().health_reset([_SRC], NOW)
    process_state().breakers.pop(_SRC, None)
    return errors


# --- ST01-03: no secret in logs, errors, results, lake or sync_slice --------------------------


def test_st01_03_sync_failing_with_401_and_500_leaks_no_secret(
    ops_store: OpsStoreHandle, lake: FakeLake, no_sleep: list[float], tmp_path: Path
) -> None:
    """ST01-03 sentinel secrets in the fake backend; a sync succeeds, then fails with 401
    (refetch + retry, then AuthError), 500 (SourceUnavailable after retries) and a refused
    token endpoint: no sentinel in captured logs (DEBUG included), exception strings and
    tracebacks, the SyncResult, lake batches, sync_slice.last_error or any written file."""
    d.store("sn", _OAUTH_SECRET)
    src = d.Source(records=[_RECORD])
    client = mock_client(src)
    settings = AuthSettings(method="oauth_password", credentials="secret:sn")
    auth = build_auth(
        settings, source=_SRC, base_url=d.BASE, token_client=client, clock=lambda: NOW
    )
    http = SourceHttp(client, breaker_key=_SRC, auth=auth, clock=lambda: NOW)
    runner = make_runner(AuthedConnector(http), servicenow_cfg(), lake, ops_store.data_root)
    set_watermark(_SRC, _ENT, "sys_updated_on", T, now=NOW)
    window = (NOW - datetime.timedelta(days=1), NOW)
    errors: list[HernessError] = []
    last_errors: list[str] = []

    with capture_logs() as logs:
        result = runner.run_incremental(_ENT)
        src.data = [401]
        with pytest.raises(AuthError) as refused:
            runner.run_backfill(_ENT, *window)
        errors.append(refused.value)
        last_errors += _slice_errors_then_close_breaker()
        src.data = [500]
        with pytest.raises(SourceUnavailable) as down:
            runner.run_backfill(_ENT, *window)
        errors.append(down.value)
        last_errors += _slice_errors_then_close_breaker()
        src.data, src.token = [401], [401]
        with pytest.raises(AuthError) as token_refused:
            runner.run_incremental(_ENT)
        errors.append(token_refused.value)

    # The sentinels were really in play: the form carried them, the data requests the token.
    assert d.form_of(src.token_calls[0])["client_secret"] == d.CLIENT_SECRET
    assert f"Bearer {d.ACCESS}1" in src.bearers()
    assert "HTTP 401 GET /api/now/table/incident" in str(refused.value)
    assert "HTTP 401 POST /oauth_token.do" in str(token_refused.value)
    assert no_sleep  # the 500 was retried before it surfaced
    assert result.rows == 1
    assert len(last_errors) == 2
    assert last_errors[0].startswith("AuthError")
    assert last_errors[1].startswith("SourceUnavailable")
    assert any(e["log_level"] == "debug" for e in logs)

    _assert_clean(repr(logs))
    for err in errors:
        _assert_clean(_error_text(err))
    _assert_clean(repr(result))
    _assert_clean(json.dumps(result.to_dict(data_root=ops_store.data_root)))
    _assert_clean(repr([b.to_pylist() for w in lake.writers for b in w.batches]))
    _assert_clean(repr(lake.events))
    _assert_clean("\n".join(last_errors))
    _assert_clean(repr(auth))
    written = _written_bytes(ops_store.data_root)
    assert b"SourceUnavailable: source unavailable" in written  # the ops store was scanned
    _assert_clean(written)
    _assert_clean(_written_bytes(tmp_path))


# --- ST01-15: expired token refreshed before use; token never logged ---------------------------


def test_st01_15_expired_oauth_token_refreshed_before_use_never_logged() -> None:
    """ST01-15 a token expired by the frozen clock is replaced before the next request; at
    DEBUG the logs carry `source` only and neither token is in the logs or the `repr`."""
    fc = FakeClock(FIXTURE_START)
    src = d.Source()
    form: dict[str, SecretStr | str] = {
        "grant_type": "client_credentials",
        "client_id": SecretStr(d.CLIENT_ID),
        "client_secret": SecretStr(d.CLIENT_SECRET),
    }
    auth = OAuthTokenAuth(f"{d.BASE}{d.TOKEN_PATH}", form, client=mock_client(src), clock=fc.now)
    client = mock_client(src)

    with capture_logs() as logs:
        client.get(d.DATA_PATH, auth=auth)
        fc.advance(601)  # past expires_in 600: expired, not merely within the margin
        client.get(d.DATA_PATH, auth=auth)

    assert len(src.token_calls) == 2
    assert src.bearers() == [f"Bearer {d.ACCESS}1", f"Bearer {d.ACCESS}2"]
    refreshed = [e for e in logs if e["event"] == "connectors.auth.token_refreshed"]
    assert [(e["log_level"], e["source"]) for e in refreshed] == [("debug", _SRC)] * 2
    assert all(set(e) == {"event", "log_level", "component", "source"} for e in refreshed)
    for text in (repr(logs), repr(auth), str(auth), repr(vars(auth))):
        assert d.ACCESS not in text
        assert d.CLIENT_SECRET not in text


def test_st01_15_expired_msal_token_refreshed_before_use_never_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST01-15 MSAL: the expired token is replaced before use; tokens absent from the
    DEBUG logs and from `repr`/`str` of the provider."""
    fc = FakeClock(FIXTURE_START)
    factory = d.MsalFactory(d.FakeMsalApp([d.msal_token(1), d.msal_token(2)]))
    monkeypatch.setattr(auth_module, "_msal_app", factory)
    provider = MsalTokenProvider(
        tenant_id=d.TENANT,
        client_id=d.CLIENT_ID,
        credential=SecretStr(d.CLIENT_SECRET),
        scope="https://org.crm.dynamics.com/.default",
        clock=fc.now,
    )
    src = d.Source()
    client = mock_client(src, base_url="https://org.crm.dynamics.com")

    with capture_logs() as logs:
        client.get(d.DATA_PATH, auth=provider)
        fc.advance(601)
        client.get(d.DATA_PATH, auth=provider)

    assert len(factory.app.calls) == 2
    assert src.bearers() == [f"Bearer {d.ACCESS}1", f"Bearer {d.ACCESS}2"]
    state = {k: v for k, v in vars(provider).items() if k != "_app"}  # the fake app scripts tokens
    for text in (repr(logs), repr(provider), str(provider), repr(state)):
        assert d.ACCESS not in text
        assert d.CLIENT_SECRET not in text
