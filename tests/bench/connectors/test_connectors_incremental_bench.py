"""Incremental sync of every source end to end (impl 01 BT01-05; design 01 §8; T01-25).

Run: pytest -m "integration and slow" tests/bench/connectors/test_connectors_incremental_bench.py.
Fixture replay of 300,000 new records across ServiceNow, Jira, Snowflake, MongoDB, Dataverse,
monitoring and files (``tests.bench.connectors._sources``): each source's production connector
runs one incremental ``SyncRunner`` pass with the real ``LakeWriter``, deletion filter, ops
store and breaker. ``HERNESS_BT01_05_RECORDS`` shrinks the total for a dry run (the 5 minute
threshold never changes).
"""

from __future__ import annotations

import os
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest
from tests.bench.connectors import _sources as src
from tests.bench.connectors._sn_load import SyntheticServiceNow
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.unit.connectors import _mongo_data as mg
from tests.unit.connectors import _servicenow_env as sn
from tests.unit.connectors import _snowflake_data as sf
from tests.unit.connectors._http_data import bind_resilience

from herness.core import config as c
from herness.core.resilience import ProcessState

pytestmark = [pytest.mark.integration, pytest.mark.slow]

RECORDS_ENV = "HERNESS_BT01_05_RECORDS"
SPEC_RECORDS = 300_000
LIMIT_S = 300.0
# share of the records per source, in thirds of a thousand (sums to 1000)
_SHARE = {
    "servicenow": 334,
    "jira": 133,
    "snowflake": 167,
    "mongodb": 100,
    "dataverse": 100,
    "monitoring": 100,
    "files": 66,
}


@pytest.fixture
def sources(
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    fake_keyring: MemoryKeyring,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[list[src.Source]]:
    """Every source built over its stand-in, secrets stored, resilience bound to the ops store."""
    del fake_keyring
    reset_process_state.sleep = lambda _s: None
    total = int(os.environ.get(RECORDS_ENV, SPEC_RECORDS))
    counts = {name: total * share // 1000 for name, share in _SHARE.items()}
    sn.store_secret()
    sf.store_credential()
    mg.store_uri(mg.URI)
    text = sn.sources_yaml().replace("page_size: 100", "page_size: 10000")
    settings = sn.load(tmp_path, text.replace("batch_rows: 1000", "batch_rows: 10000"))
    bind_resilience()
    sn.install(monkeypatch, sn.SnHost(SyntheticServiceNow(counts["servicenow"])))
    root = ops_store.data_root
    yield [
        src.servicenow(settings, counts["servicenow"], root),
        src.jira(counts["jira"], root, monkeypatch),
        src.snowflake(counts["snowflake"], root),
        src.mongodb(counts["mongodb"], root),
        src.dataverse(counts["dataverse"], root),
        src.monitoring_events(counts["monitoring"], root),
        src.files(counts["files"], tmp_path, root),
    ]
    c.reset_config()


def test_bt01_05_incremental_sync_of_300k_records_across_all_sources(
    sources: list[src.Source],
) -> None:
    """BT01-05 an incremental fixture replay of 300k new records across all sources
    (ServiceNow, Jira, Snowflake, MongoDB, Dataverse, monitoring, files) takes under 5 min
    end to end and commits every record."""
    expected = sum(s.rows for s in sources)
    committed: dict[str, int] = {}
    start = time.perf_counter()
    for source in sources:
        committed[source.name] = source.run()
    seconds = time.perf_counter() - start
    sys.stderr.write(
        f"BT01-05 {expected:,} records {seconds:.1f}s (limit {LIMIT_S:.0f}s) {committed}\n"
    )
    assert committed == {s.name: s.rows for s in sources}
    assert seconds < LIMIT_S, f"{seconds:.1f}s >= {LIMIT_S:.0f}s"
