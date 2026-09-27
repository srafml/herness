"""Deleted records never reach a lake file (impl 01 ST01-11, TH01-11; U01-33, U01-44; T01-13).

A files sync with the real `LakeWriter` over a delta entity and a snapshot entity whose
batches each carry one record under a `running` deletion request. Every file written under
`data/raw` is then scanned twice: its raw bytes (Parquet statistics, footers, uncompressed
pages) and its decoded cells (every column, `_payload` included, after decompression).
Neither the deleted `record_id`, nor its source key, nor its other field values appear;
the kept records' ids do appear in both scans (positive control), so the scans can see ids.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Iterator
from pathlib import Path

import pyarrow.parquet as pq
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import drop_inbox, init_sync_config

from herness.connectors.files import FilesConnector
from herness.connectors.jobs import handle_sync
from herness.core import config as c
from herness.core import registry
from herness.core.resilience import ProcessState
from herness.store.ops.privacy import create_deletion_request, set_deletion_status

pytestmark = pytest.mark.unit

SOURCES_YAML = """\
version: 1
sources:
  files:
    enabled: true
    inbox: data/inbox
    reconcile: {max_delete_pct: 90}
    entities:
      teams: {pattern: "*.csv", key_field: [team_code]}
      roster: {pattern: "*.csv", key_field: [member_code], mode: snapshot}
"""
_NOW = datetime.datetime(2026, 3, 1, tzinfo=datetime.UTC)
DELETED = {"teams": ("ZQDEL4471X", "zq-gone-team-8812"), "roster": ("ZQDEL5582Y", "zq-gone-m-9923")}
KEPT = {"teams": ("T1", "T3"), "roster": ("R1", "R3")}


def _csv(key_col: str, rows: list[tuple[str, str]]) -> str:
    return f"{key_col},name\n" + "".join(f"{k},{n}\n" for k, n in rows)


@pytest.fixture
def data(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> Iterator[Path]:
    """Files-only config over the ops store's data root, files connector registered."""
    del reset_process_state, fake_keyring
    assert init_sync_config(tmp_path, SOURCES_YAML).paths.data == ops_store.data_root
    registry.register("connector", "files")(FilesConnector)
    yield ops_store.data_root
    c.reset_config()


def _delete(record_id: str) -> None:
    request = create_deletion_request(
        record_id=record_id, requested_by="a" * 32, reason_ref="ticket-1", now=_NOW
    )
    set_deletion_status(request.request_id, "running")


def _sync() -> list[dict[str, object]]:
    outcome = handle_sync(FakeJobContext({"source": "files"}, kind="sync"))
    assert outcome.status == "done", outcome
    results = outcome.result["results"]
    assert isinstance(results, list)
    return [dict(r) for r in results]  # type: ignore[arg-type]


def _lake_files(data: Path) -> list[Path]:
    return sorted(p for p in (data / "raw").rglob("*") if p.is_file())


def _decoded(path: Path) -> str:
    """Every cell of every column of the Parquet file, as one string."""
    return repr(pq.read_table(path).to_pylist())


def test_st01_11_deleted_record_id_bytes_are_in_no_lake_file(data: Path) -> None:
    """ST01-11 a deleted `record_id` in a batch: the id's bytes do not appear in any written
    lake file (delta write, snapshot write and snapshot reconcile tombstones)."""
    teams_key, teams_name = DELETED["teams"]
    roster_key, roster_name = DELETED["roster"]
    teams = [("T1", "Platform"), (teams_key, teams_name), ("T3", "Search")]
    roster = [("R1", "Ann"), (roster_key, roster_name), ("R3", "Cy")]
    drop_inbox(data, "teams/teams.csv", _csv("team_code", teams))
    drop_inbox(data, "roster/a.csv", _csv("member_code", roster))
    _delete(f"files:teams:{teams_key}")
    _delete(f"files:roster:{roster_key}")

    first = {r["entity"]: r for r in _sync()}
    assert (first["teams"]["rows"], first["teams"]["skipped_deleted"]) == (2, 1)
    assert (first["roster"]["rows"], first["roster"]["skipped_deleted"]) == (2, 1)
    # a newer snapshot without R3: reconcile writes a tombstone file for R3
    later = drop_inbox(data, "roster/b.csv", _csv("member_code", [("R1", "Ann")]))
    stamp = later.stat().st_mtime + 60
    os.utime(later, (stamp, stamp))
    second = {r["entity"]: r for r in _sync()}
    assert second["roster"]["tombstones"] == 1

    files = _lake_files(data)
    assert len(files) >= 3  # teams write, roster write, roster rewrite + tombstone
    assert all(p.suffix == ".parquet" and not p.name.startswith(".") for p in files)
    needles = [
        text.encode("utf-8")
        for entity, (key, name) in DELETED.items()
        for text in (f"files:{entity}:{key}", key, name)
    ]
    decoded = "".join(_decoded(p) for p in files)
    for path in files:
        raw = path.read_bytes()
        assert not [n for n in needles if n in raw], path.name
    assert not [n for n in needles if n.decode("utf-8") in decoded]
    raw_all = b"".join(p.read_bytes() for p in files)
    for entity, keys in KEPT.items():  # positive control: both scans see the kept ids
        for key in keys:
            assert f"files:{entity}:{key}" in decoded
            assert f"files:{entity}:{key}".encode() in raw_all
