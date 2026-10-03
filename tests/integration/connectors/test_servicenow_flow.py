"""Integration flows of the ServiceNow connector (impl 01 IT01-02 to IT01-06, IT01-10; flows
F01-01 to F01-03; T01-16).

Real pieces: the loaded config, the migrated ops store, the production-built connector (egress
source client with its host guard, ``build_auth`` OAuth), ``SyncRunner`` with backfill and
reconciliation, the real ``LakeWriter`` and the T02-13 staging build
(``110_stg_servicenow.sql``) on a temp DuckDB build through ``build_harness``. The table
state is the seeded ``incident_table`` cassette served by ``FakeServiceNow`` behind a mock
pool transport (``_servicenow_env``); records change between runs as each row describes.
"""

from __future__ import annotations

import datetime
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from structlog.testing import capture_logs
from tests.integration.model._stg_lake import build
from tests.support.build_harness import BuildHarness
from tests.support.fake_keyring import MemoryKeyring
from tests.support.ops_store import OpsStoreHandle
from tests.support.sn_cassettes import T0, FakeServiceNow, pair, sn_time
from tests.unit.connectors import _servicenow_env as env
from tests.unit.connectors._http_data import bind_resilience

from herness.connectors.runner import SyncRunner
from herness.connectors.settings import ServiceNowSettings
from herness.core import config as c
from herness.core.resilience import ProcessState
from herness.store.ops import get_watermark, set_watermark

pytestmark = pytest.mark.integration

_ENT = "incident"
H = datetime.timedelta(hours=1)
INC_ENUM = {"servicenow.incident_state": {"1": "open", "2": "in_progress", "6": "resolved"}}
INC_ENUM["servicenow.incident_state"] |= {"7": "closed"}


@pytest.fixture
def sn(
    fake_keyring: MemoryKeyring,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[Flow]:
    """The flow environment over the `incident_table` cassette."""
    del fake_keyring
    reset_process_state.sleep = lambda _s: None
    env.store_secret()
    settings = env.load(tmp_path)
    bind_resilience()
    fake = FakeServiceNow.from_cassette("incident_table")
    env.install(monkeypatch, env.SnHost(fake))
    yield Flow(settings, fake, env.Clock(), ops_store.data_root, tmp_path)
    c.reset_config()


class Flow:
    """One table state, a settable clock and a runner per settings."""

    def __init__(
        self,
        settings: ServiceNowSettings,
        fake: FakeServiceNow,
        clock: env.Clock,
        data_root: Path,
        root: Path,
    ) -> None:
        self.settings, self.fake, self.clock = settings, fake, clock
        self.data_root, self.root = data_root, root
        self.runner: SyncRunner = env.runner(settings, clock, data_root)

    def reload(self, yaml_text: str) -> None:
        """A changed `sources.yaml`: new section, new connector and runner."""
        self.settings = env.load(self.root, yaml_text)
        self.runner = env.runner(self.settings, self.clock, self.data_root)

    def record(self, index: int) -> dict[str, Any]:
        rec: dict[str, Any] = self.fake.tables["incident"][index]
        return rec

    def key(self, index: int) -> str:
        return str(self.record(index)["sys_id"]["value"])

    def incident_queries(self) -> list[str]:
        return [
            str(r.url.params.get("sysparm_query")) for r in self.fake.table_requests("incident")
        ]


def _stg(harness: BuildHarness) -> list[tuple[Any, ...]]:
    build(harness, enums=INC_ENUM)
    return harness.query(
        'SELECT source_key, "number", state, source_updated_at FROM stg.sn_incident ORDER BY 1'
    )


def _versions(data_root: Path, key: str) -> list[dict[str, object]]:
    return [r for r in env.lake_rows(data_root) if r["_source_key"] == key]


# --- IT01-02: a record updated inside the overlap -------------------------------------------


def test_it01_02_record_updated_inside_the_overlap(sn: Flow, build_harness: BuildHarness) -> None:
    """IT01-02 after run 1 a record is updated with a `sys_updated_on` inside the overlap
    (before the watermark): run 2 re-reads the overlap and lands the new version in the lake;
    the staging build keeps one row for the record, the new one."""
    assert build_harness.layout.raw == sn.data_root / "raw"
    first = sn.runner.run_incremental(_ENT)  # no watermark: backfill from 2026-02-20
    wm = get_watermark("servicenow", _ENT)
    assert wm is not None
    assert first.rows == 244
    assert wm.value == T0 + 59 * H + 45 * datetime.timedelta(minutes=1)

    rec = sn.record(236)
    rec["sys_updated_on"] = pair(sn_time(wm.value - 15 * datetime.timedelta(minutes=1)))
    rec["number"] = pair("INC0099999")
    rec["state"] = pair("6", "Resolved")
    sn.fake.tables["incident"].sort(key=lambda r: r["sys_updated_on"]["value"])
    sn.clock.now += H
    second = sn.runner.run_incremental(_ENT)

    assert second.mode == "incremental"
    assert sn.incident_queries()[-1].startswith(f"sys_updated_on>={sn_time(wm.value - H)}^")
    versions = _versions(sn.data_root, str(rec["sys_id"]["value"]))
    assert [v["number"] for v in versions] == ["INC0010236", "INC0099999"]
    rows = _stg(build_harness)
    assert len(rows) == 240  # the 4 audited deletes name keys that were never live
    (row,) = [r for r in rows if r[0] == str(rec["sys_id"]["value"])]
    assert row[1:3] == ("INC0099999", "resolved")


# --- IT01-03: the same cassette twice ---------------------------------------------------------


def _core_counts(harness: BuildHarness) -> dict[str, int]:
    build(harness, enums=INC_ENUM, hi=299)
    tables = harness.query(
        "SELECT table_name FROM information_schema.tables WHERE table_schema = 'core' ORDER BY 1"
    )
    return {
        str(t): int(str(harness.query(f'SELECT count(*) FROM core."{t}"')[0][0]))  # noqa: S608
        for (t,) in tables
    }


def test_it01_03_unchanged_cassette_twice_gives_identical_core_counts(
    sn: Flow, build_harness: BuildHarness
) -> None:
    """IT01-03 the unchanged cassette synced twice (run 2 re-reads the overlap): the lake
    holds the re-read versions, yet after dedupe the `core.*` row counts are identical."""
    sn.runner.run_incremental(_ENT)
    after_one = _core_counts(build_harness)
    lake_one = len(env.lake_rows(sn.data_root))
    sn.clock.now += H
    second = sn.runner.run_incremental(_ENT)
    assert second.rows == 5  # the overlap hour re-read (since inclusive): nothing new
    assert len(env.lake_rows(sn.data_root)) == lake_one + 5
    assert _core_counts(build_harness) == after_one
    assert after_one["incident"] == 240


# --- IT01-04: a new field, a removed field, a type change ------------------------------------


def test_it01_04_new_removed_and_retyped_fields(sn: Flow, build_harness: BuildHarness) -> None:
    """IT01-04 run 2 configures a new field (`short_description`) and drops one (`priority`)
    and a record's `state` arrives as an object: run 2 writes through a new writer whose files
    carry the new column set; old and new files coexist; the staging build succeeds, the
    retyped value is a counted cast failure and the record keeps its new field."""
    sn.runner.run_incremental(_ENT)
    files_one = env.lake_files(sn.data_root)
    rec = sn.record(238)
    rec["state"] = {"value": {"code": 6}, "display_value": "Resolved"}
    sn.reload(
        env.sources_yaml(fields="[number, opened_at, state, assignment_group, short_description]")
    )
    sn.clock.now += H
    with capture_logs() as logs:
        second = sn.runner.run_incremental(_ENT)
    files_two = [p for p in env.lake_files(sn.data_root) if p not in files_one]
    assert len(files_one) == 1
    assert len(files_two) == 1
    assert {Path(p).resolve() for p in second.files} == {p.resolve() for p in files_two}

    names_one = set(env.pq.read_schema(files_one[0]).names)
    names_two = set(env.pq.read_schema(files_two[0]).names)
    assert names_two - names_one == {"short_description", "short_description_display"}
    assert names_one - names_two == {"priority", "priority_display"}
    (retyped,) = [r for r in env.lake_rows(sn.data_root) if r["state"] == '{"code":6}']
    assert retyped["state_display"] == "Resolved"
    assert retyped["short_description"] == "Synthetic incident 10238"
    completed = [e for e in logs if e["event"] == "connectors.sync.completed"]
    assert [e["rows"] for e in completed] == [5]

    rows = _stg(build_harness)
    assert len(rows) == 240
    (row,) = [r for r in rows if r[0] == str(rec["sys_id"]["value"])]
    assert row[2] is None  # the object state does not cast
    described = build_harness.query(
        "SELECT short_description, priority FROM stg.sn_incident WHERE source_key = ?",
        [str(rec["sys_id"]["value"])],
    )
    assert described == [("Synthetic incident 10238", None)]


# --- IT01-05: reconciliation tombstones a key the source no longer lists ----------------------


def test_it01_05_reconcile_tombstones_a_missing_key(sn: Flow, build_harness: BuildHarness) -> None:
    """IT01-05 the lake of a first run, then a key listing missing one key (deleted at the
    source without an audit row): reconcile writes one tombstone; the record is gone from
    staging; the watermark does not move."""
    sn.runner.run_incremental(_ENT)
    wm = get_watermark("servicenow", _ENT)
    gone = sn.fake.tables["incident"].pop(100)
    key = str(gone["sys_id"]["value"])
    assert len(_stg(build_harness)) == 240
    sn.clock.now += H

    result = sn.runner.run_reconcile(_ENT)

    assert (result.mode, result.rows, result.tombstones) == ("reconcile", 1, 1)
    (tomb,) = [r for r in _versions(sn.data_root, key) if r["_deleted"]]
    assert tomb["_source_updated_at"] == sn.clock.now
    reads = sn.fake.table_requests("incident")[-3:]
    assert {r.url.params["sysparm_fields"] for r in reads} == {"sys_id"}
    assert get_watermark("servicenow", _ENT) == wm
    rows = _stg(build_harness)
    assert len(rows) == 239
    assert key not in {r[0] for r in rows}


# --- IT01-06: backfill to now, then an incremental -------------------------------------------


def test_it01_06_backfill_then_incremental_has_no_gap(sn: Flow) -> None:
    """IT01-06 a backfill with `end` = now, then an incremental: its first `since` is the
    backfill watermark minus the overlap, and a record updated after the backfill's last
    record arrives (no gap)."""
    sn.runner.run_backfill(_ENT, T0, sn.clock.now)
    wm = get_watermark("servicenow", _ENT)
    assert wm is not None
    assert wm.value == T0 + 59 * H + datetime.timedelta(minutes=45)  # last committed record
    late = dict(sn.record(0))
    late["sys_id"] = pair("feed" + "0" * 24 + "0001")
    late["sys_updated_on"] = pair(sn_time(T0 + 61 * H))
    sn.fake.tables["incident"].append(late)
    before = len(sn.incident_queries())
    sn.clock.now += H

    second = sn.runner.run_incremental(_ENT)

    first_query = sn.incident_queries()[before]
    assert first_query.startswith(f"sys_updated_on>={sn_time(wm.value - H)}^")
    assert second.mode == "incremental"
    keys = {r["_source_key"] for r in env.lake_rows(sn.data_root) if not r["_deleted"]}
    assert keys == {str(r["sys_id"]["value"]) for r in sn.fake.tables["incident"]}
    after = get_watermark("servicenow", _ENT)
    assert after is not None
    assert after.value == T0 + 61 * H


# --- IT01-10: a record moves during paging ----------------------------------------------------


def test_it01_10_record_moved_during_paging_arrives_next_run(sn: Flow) -> None:
    """IT01-10 while run 1 pages a 48 h window, a record on its second page is updated (its
    `sys_updated_on` moves past run 1's `until`): it is missing after run 1 (no other record
    is lost to the offset shift) and present after run 2."""
    set_watermark("servicenow", _ENT, "sys_updated_on", T0 + H, now=sn.clock.now)
    moved = sn.record(150)
    key = str(moved["sys_id"]["value"])
    done: list[int] = []

    def update_once(table: str, offset: int) -> None:
        if table == "incident" and offset == 0 and not done:
            done.append(offset)
            stamp = sn_time(sn.clock.now - datetime.timedelta(seconds=30))
            moved["sys_updated_on"] = pair(stamp)
            sn.fake.tables["incident"].sort(key=lambda r: r["sys_updated_on"]["value"])

    sn.fake.after_page = update_once
    sn.runner.run_incremental(_ENT)
    live = {r["_source_key"] for r in env.lake_rows(sn.data_root) if not r["_deleted"]}
    assert key not in live
    assert len(live) == 239
    assert done == [0]

    sn.clock.now += H
    sn.runner.run_incremental(_ENT)
    versions = _versions(sn.data_root, key)
    assert [v["_source_updated_at"] for v in versions] == [
        sn.clock.now - H - datetime.timedelta(seconds=30)
    ]
