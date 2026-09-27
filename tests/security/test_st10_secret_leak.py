"""Security test ST10-14 (impl 10, card T10-21, TH10-07): the sentinel grep over IT10-10.

The IT10-10 run (``tests.support.secret_leak``) plants a keyring secret (a known value), a
CREDENTIAL-shaped and a URL_TOKEN-shaped value at the connector and a blocked host in the
connector config. Nothing of it may reach ``data/logs/``, ``data/traces/``,
``data/config_snapshots/``, an ``ops.sqlite`` ``.dump``, rendered reports (no renderer exists
on the tree yet: ``data/reports/`` is scanned when present), the model requests or the review
items; the blocked host never gets a socket connection.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.secret_leak import (
    BLOCKED_HOST,
    SECRET_NAME,
    SENTINELS,
    LeakRun,
    artefacts,
    run_sentinel_pipeline,
)

from herness.core.resilience import ProcessState

pytestmark = pytest.mark.integration

_MODEL = ("model requests (FakeLLMClient)", "model requests (wire)")
# Categories that must be non-empty for the grep to prove anything (reports: no renderer yet).
_REQUIRED = (
    "data/logs",
    "data/traces",
    "data/config_snapshots",
    "ops.sqlite .dump",
    *_MODEL,
    "review items",
)


@pytest.fixture
def leak_run(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> LeakRun:
    """The IT10-10 sentinel run over the fixture ops store."""
    del fake_keyring, reset_process_state
    return run_sentinel_pipeline(tmp_path, ops_store.db_path, monkeypatch)


def test_st10_14_zero_sentinel_hits_in_every_artefact(leak_run: LeakRun) -> None:
    """ST10-14 zero sentinel hits in logs, traces, snapshots, the ops dump, reports, model
    requests and review items."""
    hits = [
        f"{category}: {name}"
        for category, items in artefacts(leak_run).items()
        for name, text in items
        for sentinel in SENTINELS
        if sentinel in text
    ]
    assert hits == []


def test_st10_14_grep_scanned_real_artefacts(leak_run: LeakRun) -> None:
    """ST10-14 the grep is not vacuous: every scanned category holds the run's content."""
    found = artefacts(leak_run)
    for category in _REQUIRED:
        assert found[category], category
        assert any(text.strip() for _, text in found[category]), category
    log_files = {name.split("-")[0] for name, _ in found["data/logs"]}
    assert {"herness", "audit"} <= log_files  # the JSONL log and the audit file
    joined = {category: "\n".join(t for _, t in items) for category, items in found.items()}
    assert "connectors.auth.failed" in joined["data/logs"]  # the line that echoed the secret
    assert "***" in joined["data/logs"]  # ... reached the file masked
    assert '"review_decision"' in joined["data/logs"]  # the audit file is scanned too
    for event in ('"llm_call"', '"tool_call"', '"retry"', '"payload"'):
        assert event in joined["data/traces"], event
    assert f"secret:{SECRET_NAME}" in joined["data/config_snapshots"]  # reference, not value
    assert "INSERT INTO" in joined["ops.sqlite .dump"]
    assert "file_ingest" in joined["ops.sqlite .dump"]
    assert "review_item" in joined["ops.sqlite .dump"]
    for category in _MODEL:
        assert "Printer outage" in joined[category], category  # the record text got there
    assert "mapping_suggestion" in joined["review items"]


def test_st10_14_blocked_host_never_connects_nor_reaches_the_model(leak_run: LeakRun) -> None:
    """ST10-14 the connector's blocked host: EgressBlocked before any connect or resolution,
    and absent from every model request."""
    assert leak_run.blocked is not None
    assert BLOCKED_HOST in str(leak_run.blocked)
    assert leak_run.connects == []
    assert leak_run.resolved_blocked is False
    found = artefacts(leak_run)
    for category in _MODEL:
        assert found[category]
        assert all(BLOCKED_HOST not in text for _, text in found[category]), category
