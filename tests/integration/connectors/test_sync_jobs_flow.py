"""Integration tests of the `sync` job handler on the files connector (impl 01 IT01-01,
IT01-08, IT01-09; flows F01-01, F01-04, F01-05; T01-11).

Real pieces: the migrated ops store, the loaded config, the registry, `build_connector`,
`SyncRunner`, the files connector and the real `LakeWriter`. The `lake_small` inbox of
T11-16 does not exist yet, so `tests.support.sync_env` writes the smallest stand-in inbox.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path
from typing import Any, cast

import pyarrow.parquet as pq
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import drop_inbox, init_sync_config

import herness.connectors.jobs as jobs_module
from herness.connectors.files import FilesConnector
from herness.connectors.jobs import build_sync_payload, handle_sync, register_job_handlers
from herness.connectors.runner import SyncResult
from herness.core import config as c
from herness.core import redact as r
from herness.core import registry
from herness.core.jobs import queue
from herness.core.jobs.handlers import Handler, register_handler, resolve_handler
from herness.core.jobs.inline import run_inline
from herness.core.jobs.ports import JobsBackend, bind_jobs_backend
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState, bind_ops_backend
from herness.core.resilience import reset_process_state as reset_state
from herness.core.settings import RedactionConfig
from herness.core.types import JobKind, JobOutcome
from herness.store.ops import read_all
from herness.store.ops.jobs import SqliteJobsBackend
from herness.store.ops.privacy import create_deletion_request, set_deletion_status
from herness.store.ops.resilience import SqliteResilienceBackend

pytestmark = pytest.mark.integration

_NOW = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)


@pytest.fixture
def env(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Path]:
    """Migrated ops store, files-only config (paths.data = the store's data root), the files
    connector registered, a fixed-key redactor and the ops backends bound; yields data root."""
    del reset_process_state, fake_keyring
    cfg = init_sync_config(tmp_path)
    assert cfg.paths.data == ops_store.data_root
    registry.register("connector", "files")(FilesConnector)
    directory = NameDirectory.from_files(None, (), None)
    redactor = r.Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(r._State, "redactor", redactor)
    bind_ops_backend(SqliteResilienceBackend())
    bind_jobs_backend(cast("JobsBackend", SqliteJobsBackend()))
    yield ops_store.data_root
    c.reset_config()


def _results(outcome: JobOutcome) -> list[dict[str, Any]]:
    assert outcome.status == "done"
    results = outcome.result["results"]
    assert isinstance(results, list)
    return cast("list[dict[str, Any]]", results)


def _file_ingest_rows() -> int:
    return len(read_all("SELECT fingerprint FROM file_ingest"))


def test_it01_01_files_sync_twice_is_idempotent(env: Path) -> None:
    """IT01-01 `handle_sync` files twice: lake files present, `file_ingest` rows, the second
    run adds 0 rows."""
    drop_inbox(env)
    (first,) = _results(handle_sync(FakeJobContext({"source": "files"}, kind="sync")))
    assert (first["source"], first["entity"], first["rows"]) == ("files", "teams", 3)
    assert first["files"]
    for rel in first["files"]:
        path = env / rel
        assert path.is_file()
        assert path.is_relative_to(env / "raw" / "files" / "teams")
    assert _file_ingest_rows() == 1
    (second,) = _results(handle_sync(FakeJobContext({"source": "files"}, kind="sync")))
    assert (second["rows"], second["files"]) == (0, [])
    assert _file_ingest_rows() == 1
    assert len(list((env / "raw" / "files" / "teams").rglob("*.parquet"))) == len(first["files"])


def test_it01_08_sync_job_runs_through_the_queue(env: Path) -> None:
    """IT01-08 registered handlers; a `sync` job for files enqueued and run through T08-22
    `run_inline` ends `done` with the `SyncResult` dict in its result."""
    drop_inbox(env)
    register_job_handlers()
    kind, payload, idem_key = build_sync_payload("files", today=_NOW.date())
    job_id = queue.enqueue(kind, payload, "none", idem_key=idem_key)
    assert queue.enqueue(kind, payload, "none", idem_key=idem_key) == job_id  # deduped
    outcome = run_inline(job_id)
    (result,) = _results(outcome)
    assert set(result) == set(SyncResult.__dataclass_fields__)
    assert (result["source"], result["entity"], result["rows"]) == ("files", "teams", 3)
    assert outcome.result["partial"] is False
    finished = queue.get(job_id)
    assert (finished.kind, finished.idem_key) == ("sync", "sync:files")
    assert (finished.status, finished.attempts, finished.lease_owner) == ("done", 1, None)
    assert finished.result == dict(outcome.result)


def test_it01_08_register_job_handlers_is_idempotent(
    reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT01-08 (U01-53) both kinds registered on the first call; the second call is a no-op
    that never reaches `register_handler` again (the module flag)."""
    seen: list[str] = []

    def spy(kind: JobKind, handler: Handler) -> None:
        seen.append(kind)
        register_handler(kind, handler)

    monkeypatch.setattr(jobs_module, "register_handler", spy)
    register_job_handlers()
    assert seen == ["sync", "reconcile"]
    register_job_handlers()
    assert seen == ["sync", "reconcile"]
    assert resolve_handler("sync") is handle_sync
    assert resolve_handler("reconcile") is jobs_module.handle_reconcile
    assert set(reset_process_state.handlers) == {"sync", "reconcile"}


def test_it01_08_registration_follows_a_reset_process_state(
    reset_process_state: ProcessState,
) -> None:
    """IT01-08 (U01-53) a replaced process state (empty handler table) counts as not
    registered, so the next call registers the handlers again."""
    register_job_handlers()
    assert reset_process_state.handlers
    fresh = reset_state()
    assert fresh.handlers == {}
    register_job_handlers()
    assert resolve_handler("sync") is handle_sync


def _request(record_id: str, *statuses: str) -> None:
    request = create_deletion_request(
        record_id=record_id, requested_by="a" * 32, reason_ref="ticket-1", now=_NOW
    )
    for status in statuses:
        set_deletion_status(request.request_id, status)  # type: ignore[arg-type]


def test_it01_09_deletion_requests_filter_the_sync(env: Path) -> None:
    """IT01-09 requests `running`, `done`, `pending` for three fetched ids: only the
    `pending` one is written; `skipped_deleted` = 2."""
    drop_inbox(env)
    _request("files:teams:T1", "running")
    _request("files:teams:T2", "running", "done")
    _request("files:teams:T3")
    (result,) = _results(handle_sync(FakeJobContext({"source": "files"}, kind="sync")))
    assert (result["rows"], result["skipped_deleted"]) == (1, 2)
    keys = [
        key
        for rel in result["files"]
        for key in pq.read_table(env / rel, columns=["_source_key"]).column(0).to_pylist()
    ]
    assert keys == ["T3"]
