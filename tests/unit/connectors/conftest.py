"""Fixtures of the sync-runner tests (T01-06): a recording fake lake and a fake breaker guard.

Imports of the runner stay inside the fixtures so the other connector tests never import it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from tests.support.fake_lake import FakeLake
from tests.support.ops_store import OpsStoreHandle

if TYPE_CHECKING:
    from tests.unit.connectors._runner_data import FakeGuard


@pytest.fixture
def lake(ops_store: OpsStoreHandle) -> FakeLake:
    """A fake `LakeWriter` factory rooted at the ops store's `data/raw`."""
    return FakeLake(ops_store.data_root / "raw")


@pytest.fixture
def guard(monkeypatch: pytest.MonkeyPatch) -> FakeGuard:
    """Replace the runner's breaker `guard`; add keys to `open_keys` to trip them."""
    from tests.unit.connectors._runner_data import FakeGuard  # noqa: PLC0415 - as above

    import herness.connectors.runner as runner_module  # noqa: PLC0415 - runner tests only

    fake = FakeGuard()
    monkeypatch.setattr(runner_module, "guard", fake)
    return fake
