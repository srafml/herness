"""Security tests for resilience events and metrics (impl 08 ST08-02 event and log part,
ST08-10 metric part; TH08-02, TH08-10; T08-05). Planted values are built at runtime so
detect-secrets stays quiet."""

from __future__ import annotations

import sys

import pytest
import structlog
from tests.support.ops_store import OpsStoreHandle

from herness.core import redact as r
from herness.core import secrets
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.resilience import metrics as m
from herness.core.resilience.events import record_event
from herness.core.settings import RedactionConfig
from herness.store.ops.core import read_all
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.unit

PLANTED_KNOWN = "Kv7" + "q2Lm9xTz" + "4Rw8Hs1Nd"  # a resolved secret with no pattern shape
EMAIL = "jane.victim" + "@examplecorp.org"
PLANTED_BEARER = "eyJhbGciOiJIUzI1NiJ9" + ".abcDEF123456789xyz"
TICKET = "customer ticket text " * 500  # ~10 KB


@pytest.fixture
def planted(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bound backend, a test redactor and one known secret value."""
    del ops_store, reset_process_state
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    monkeypatch.setattr(secrets, "_KNOWN", {PLANTED_KNOWN})
    monkeypatch.setattr(secrets, "_KNOWN_VERSION", secrets._KNOWN_VERSION + 1)
    bind_ops_backend(SqliteResilienceBackend())


def test_st08_02_planted_values_never_reach_event_rows_or_logs(planted: None) -> None:
    """ST08-02 a known secret, an e-mail, a bearer header and 10 KB of ticket text in event
    detail reach neither resilience_event.detail nor the captured log lines."""
    del planted
    error = f"ModelUnavailable: {PLANTED_KNOWN} for {EMAIL} Authorization: Bearer {PLANTED_BEARER}"
    with structlog.testing.capture_logs() as logs:
        record_event(
            "fallback",
            component="resilience",
            detail={"from_profile": "local", "reason": error, "to_profile": TICKET},
        )
        record_event("job_failed", component="jobs", job_id="job_1", detail={"error_type": error})
        record_event("repair", component="resilience", detail={"error_paths": [error, TICKET]})
    details = [row["detail"] for row in read_all("SELECT detail FROM resilience_event")]
    assert len(details) == 3
    text = "\n".join(details) + "\n" + "\n".join(str(line) for line in logs)
    for value in (PLANTED_KNOWN, EMAIL, PLANTED_BEARER, TICKET[:400]):
        assert value not in text
    assert all(len(d) <= 2 * 200 + 64 for d in details)


def test_st08_10_metric_buffer_caps_20000_observations(
    reset_process_state: ProcessState,
) -> None:
    """ST08-10 20 000 histogram observations are capped at 10 000; 2 000 gauge keys at
    1 000; the rest is counted as dropped, so the buffer stays bounded."""
    for i in range(20_000):
        m.record_histogram("herness_jobs_queue_wait_seconds", float(i), component="jobs")
    for i in range(2000):
        m.record_gauge(
            "herness_jobs_queue_depth_count", 1, component="jobs", labels={"gpu_class": f"c{i}"}
        )
    buffer = reset_process_state.metric_buffer
    assert len(buffer.histograms) == m.METRIC_BUFFER_MAX == 10_000
    assert len(buffer.gauges) == m.METRIC_GAUGE_KEYS_MAX == 1000
    assert buffer.dropped == 11_000
    assert sys.getsizeof(buffer.histograms) < 200_000
