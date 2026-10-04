"""Fixtures of the sync-runner tests (T01-06): a recording fake lake and a fake breaker guard;
of the MongoDB tests (T01-22): the URI secret and a spy breaker; of the Snowflake tests
(T01-23): the key-pair credential and a spy breaker.

Imports of the runner stay inside the fixtures so the other connector tests never import it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle

from herness.core.resilience import ProcessState

if TYPE_CHECKING:
    from tests.unit.connectors._mongo_data import SpyBreaker
    from tests.unit.connectors._runner_data import FakeGuard


@pytest.fixture
def lake(ops_store: OpsStoreHandle) -> FakeLake:
    """A fake `LakeWriter` factory rooted at the ops store's `data/raw`."""
    return FakeLake(ops_store.data_root / "raw")


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> FakeGuard:
    """Replace the breaker `guard` of the runner, the backfill slices and reconciliation;
    add keys to `open_keys` to trip them."""
    from tests.unit.connectors._runner_data import FakeGuard  # noqa: PLC0415 - as above

    import herness.connectors.backfill as backfill_module  # noqa: PLC0415 - runner tests only
    import herness.connectors.reconcile as reconcile_module  # noqa: PLC0415 - runner tests only
    import herness.connectors.runner as runner_module  # noqa: PLC0415 - runner tests only

    fake = FakeGuard()
    monkeypatch.setattr(runner_module, "guard", fake)
    monkeypatch.setattr(backfill_module, "guard", fake)
    monkeypatch.setattr(reconcile_module, "guard", fake)
    return fake


@pytest.fixture
def mongo_uri(fake_keyring: MemoryKeyring) -> Callable[[str], None]:
    """MongoDB tests (T01-22): the in-memory keyring holds `secret:mongo_uri` (default
    `_mongo_data.URI`); call the returned function to replace it."""
    from tests.unit.connectors._mongo_data import URI, store_uri  # noqa: PLC0415 - mongo only

    del fake_keyring
    store_uri(URI)
    return store_uri


@pytest.fixture
def mongo_breaker(
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
    mongo_uri: Callable[[str], None],
) -> SpyBreaker:
    """MongoDB tests (T01-22): no ops store; `retry_page` guards and records on a spy breaker,
    sleeps are no-ops and the URI secret is stored."""
    from tests.unit.connectors._mongo_data import SpyBreaker  # noqa: PLC0415 - mongo only

    import herness.core.resilience.retry as retry_module  # noqa: PLC0415 - mongo only

    del reset_process_state, mongo_uri
    spy = SpyBreaker()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors.http.breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors._auth_breaker.breaker", lambda _key: spy)
    return spy


@pytest.fixture
def snowflake_env(
    monkeypatch: pytest.MonkeyPatch,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> SpyBreaker:
    """Snowflake tests (T01-23): the in-memory keyring holds `secret:snowflake_svc` (user and a
    run-time PEM key); `retry_page` guards and records on a spy breaker, sleeps are no-ops."""
    from tests.unit.connectors._mongo_data import SpyBreaker  # noqa: PLC0415 - snowflake only
    from tests.unit.connectors._snowflake_data import (  # noqa: PLC0415 - snowflake only
        store_credential,
    )

    import herness.core.resilience.retry as retry_module  # noqa: PLC0415 - snowflake only

    del reset_process_state, fake_keyring
    store_credential()
    spy = SpyBreaker()
    monkeypatch.setattr(retry_module, "guard", lambda _key: None)
    monkeypatch.setattr(retry_module, "breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors.http.breaker", lambda _key: spy)
    monkeypatch.setattr("herness.connectors._auth_breaker.breaker", lambda _key: spy)
    return spy
