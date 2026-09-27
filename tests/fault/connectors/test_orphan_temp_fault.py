"""Orphan temp-file cleanup end to end (impl 01 FT01-07; U01-34, TH01-08; T01-13).

Two crash leftovers named like the real `LakeWriter` temp files
(`.part-<ulid>.parquet.tmp-<ulid>`, valid Parquet holding rows no committed file has) sit
beside the committed lake file of `files/teams`, aged 2 h and 10 min. The runner's
start-of-run cleanup deletes only the 2 h file; the build globs neither (their rows never
reach staging, `core.*` is unchanged).
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from pathlib import Path

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
import pytest
from tests.fault.connectors._files_env import (
    build,
    committed_files,
    load,
    open_build,
    staged_teams,
    write_config,
)
from tests.support.build_harness import FakeJobContext
from tests.support.fake_keyring import MemoryKeyring
from tests.support.lake_small import GOLDEN_COUNTS, install
from tests.support.ops_store import OpsStoreHandle
from tests.support.sync_env import drop_inbox

from herness.connectors.files import FilesConnector
from herness.connectors.jobs import handle_sync
from herness.connectors.lakefiles import _TEMP_RE
from herness.core import config as c
from herness.core import registry
from herness.core.ids import new_ulid
from herness.core.resilience import ProcessState

pytestmark = pytest.mark.fault

KEYS = [("files:teams:T1", "T1"), ("files:teams:T2", "T2"), ("files:teams:T3", "T3")]
TWO_HOURS_S = 2 * 3600
TEN_MINUTES_S = 10 * 60


@pytest.fixture
def data(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
) -> Iterator[Path]:
    """Loaded config, `lake_small` installed, files connector registered; the data root."""
    del reset_process_state, fake_keyring
    assert load(write_config(tmp_path)).paths.data == ops_store.data_root
    install(ops_store.data_root)
    registry.register("connector", "files")(FilesConnector)
    yield ops_store.data_root
    c.reset_config()


def _sync() -> list[dict[str, object]]:
    outcome = handle_sync(FakeJobContext({"source": "files"}, kind="sync"))
    assert outcome.status == "done"
    results = outcome.result["results"]
    assert isinstance(results, list)
    return [dict(r) for r in results]  # type: ignore[arg-type]


def _orphan(beside: Path, key: str, age_s: int) -> Path:
    """A temp file in `beside`'s partition, named like `LakeWriter`'s, holding row `key`."""
    table = pq.read_table(beside).slice(0, 1)
    table = table.set_column(
        table.schema.get_field_index("_source_key"), "_source_key", pa.array([key])
    )
    index = table.schema.get_field_index("_record_id")
    table = table.set_column(index, "_record_id", pa.array([f"files:teams:{key}"]))
    path = beside.parent / f".part-{new_ulid()}.parquet.tmp-{new_ulid()}"
    assert _TEMP_RE.fullmatch(path.name) is not None  # the cleanup's own naming rule
    pq.write_table(table, path)
    past = time.time() - age_s
    os.utime(path, (past, past))
    return path


def _check_build(data: Path) -> None:
    outcome = build()
    assert outcome.result["row_counts"] == json.loads(GOLDEN_COUNTS.read_text(encoding="utf-8"))
    with open_build(data, str(outcome.result["build_id"])) as con:
        assert staged_teams(con) == KEYS  # no ORPHAN row: neither temp file was read


def test_ft01_07_runner_deletes_only_the_old_orphan_and_build_ignores_both(data: Path) -> None:
    """FT01-07 `.x.parquet.tmp-<ulid>` files aged 2 h and 10 min: the runner deletes the
    2 h file only; the build ignores both."""
    drop_inbox(data)
    (first,) = _sync()
    assert first["rows"] == 3
    [committed] = committed_files(data)
    old = _orphan(committed, "ORPHAN_OLD", TWO_HOURS_S)
    young = _orphan(committed, "ORPHAN_YOUNG", TEN_MINUTES_S)
    assert pc.sum(pq.read_table(old).column("_deleted")).as_py() in (0, None)  # a live row

    _check_build(data)  # before the cleanup: both present, neither globbed
    assert old.is_file()
    assert young.is_file()

    (second,) = _sync()  # the runner cleans orphans once at the start of the run
    assert (second["rows"], second["files"]) == (0, [])
    assert not old.exists()
    assert young.is_file()
    assert committed_files(data) == [committed]

    _check_build(data)  # after the cleanup: the young orphan is still ignored
    assert young.is_file()
