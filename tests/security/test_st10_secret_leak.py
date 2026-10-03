"""Security test ST10-14 (impl 10, card T10-21, TH10-07): the sentinel grep over IT10-10.

The IT10-10 run (``tests.support.secret_leak``, fixture ``leak_run``) plants a keyring secret (a
known value), a CREDENTIAL-shaped and a URL_TOKEN-shaped value at the connector and a blocked
host in the connector config. Nothing of it may reach ``data/logs/``, ``data/traces/``,
``data/config_snapshots/``, an ``ops.sqlite`` ``.dump``, rendered reports, the model requests or
the review items; the blocked host never gets a socket connection.

Sinks with a production path for a sentinel: logs, traces, the model requests, and the ops dump
(a ``resilience_event`` row from ``record_event`` and a failed job's ``last_error`` from
``finish_job``). Structural only: review items (production payloads carry ids, scores and
counts, never ticket text, TH03-03), config snapshots (references only) and rendered reports
(no renderer on the tree yet: ``data/reports/`` is scanned when present).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.secret_leak import (
    BLOCKED_HOST,
    SECRET_NAME,
    SENTINEL_KEY,
    SENTINELS,
    LeakRun,
    artefacts,
    known_secret_job_dump,
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
    dump = joined["ops.sqlite .dump"]
    for table in ("file_ingest", "review_item", "resilience_event", "job"):
        assert f'INSERT INTO "{table}"' in dump, table
    assert "upstream rejected credential ***" in dump  # the breaker_open reason, masked
    assert "reconcile failed: password=" in dump  # the failed job's last_error, redacted
    for category in _MODEL:
        assert "Printer outage" in joined[category], category  # the record text got there
    assert "mapping_suggestion" in joined["review items"]


def test_st10_14_blocked_host_never_gets_a_socket(leak_run: LeakRun) -> None:
    """ST10-14 the connector's blocked host: EgressBlocked before any connect or resolution.

    The host lives only in connector config, which no production path puts into a prompt, so
    a "never in a model request" check would be true by construction and is not asserted.
    """
    assert leak_run.blocked is not None
    assert BLOCKED_HOST in str(leak_run.blocked)
    assert leak_run.connects == []
    assert leak_run.resolved_blocked is False


def test_st10_14_job_error_with_known_secret_is_masked_in_ops(
    ops_store: OpsStoreHandle,
    fake_keyring: MemoryKeyring,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ST10-14 a failed job whose error echoes the resolved keyring secret: the ops dump holds
    the job row but not the secret value."""
    del fake_keyring, reset_process_state
    dump = known_secret_job_dump(tmp_path, ops_store.db_path, monkeypatch)
    assert "upstream rejected credential" in dump  # the job row is there
    assert SENTINEL_KEY not in dump
