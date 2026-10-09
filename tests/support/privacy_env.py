"""`privacy_env` fixture: every store a privacy deletion touches, planted under one data root
(impl 10 T10-29; IT10-06, FT10-03, ST10-50, ST10-57).

Spec 11 ``tiny_build`` is not in the tree, so (ruling T10-29) each test plants its own fixture
in ``tmp_path``: a raw lake written by the real ``LakeWriter`` through the real
``DeletionFilter`` (a "fixture sync"), enrichment vectors, decision cache and labels (the
impl 03 purge helpers), a promoted ``CURRENT`` warehouse build holding the record's
description, memory items citing the record on the real ``MemoryStore`` (vectors in
``data/vectors``), ``evidence`` and ``finding`` rows, and trace files. Impl 08 is the real
jobs backend; the rebuild is a fake job completion (``complete_build``).
"""

from __future__ import annotations

import datetime
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.unit.enrich._embed_support import SETTINGS_SQL
from tests.unit.enrich._purge_support import PurgeEnv, purge_env
from tests.unit.harness.memory._write_env import (
    KIND_DATA,
    PATTERNS,
    FakeEmbed,
    proposal,
    provenance,
)

from herness.admin import _privacy_steps as steps
from herness.admin import privacy
from herness.connectors.deletion import DeletionFilter
from herness.core import redact
from herness.core import time as clock
from herness.core.config import get_config, reset_config
from herness.core.jobs.ports import JobContext, JobRow, require_jobs_backend
from herness.core.redact import Redactor
from herness.core.redact_directory import NameDirectory
from herness.core.resilience import ProcessState
from herness.core.settings import RedactionConfig
from herness.core.types import JobOutcome
from herness.harness.memory import MemoryStore
from herness.harness.memory.settings import MemoryConfig
from herness.harness.memory.store import Embedder, VectorIndex
from herness.store import _warehouse_rw, warehouse
from herness.store.lake import LakeWriter
from herness.store.lake_purge import purge_record_ids
from herness.store.layout import data_layout
from herness.store.ops import core, reset_connections
from herness.store.ops.migrate import migrate
from herness.store.ops.privacy import DeletionRequest, open_deletion_request
from herness.store.ops.resilience import bind_core_backends

RID = "servicenow:incident:INC0042"
OTHER = "servicenow:incident:INC0043"
DESC = "zephyrquokka marmalade lighthouse"  # the record's unique description text
OTHER_DESC = "ordinary printer jam"
ACTOR = "a" * 32
NOW = datetime.datetime(2026, 10, 2, 9, 0, tzinfo=datetime.UTC)
LATER = NOW + datetime.timedelta(minutes=31)
OLD_BUILD = "20261001-120000-ABCDEF"
NEW_BUILD = "20261002-093000-ABCDEF"  # the rebuild, after the step-5 time NOW
H_RID, H_OTHER = "1" * 32, "2" * 32
BASE: dict[str, Any] = {
    "action": "privacy_delete",
    "record_id": RID,
    "reason_ref": "DSR-42",
    "requested_by": ACTOR,
}
_TS = pa.timestamp("us", tz="UTC")


def ctx(payload: dict[str, Any]) -> JobContext:
    """The ``JobContext`` part run_privacy_delete reads: ``job.payload`` (R-42)."""
    return cast("JobContext", SimpleNamespace(job=SimpleNamespace(payload=payload)))


def _lake_batch(rows: list[tuple[str, str, int]]) -> pa.RecordBatch:
    """Lake rows ``(record_id, description, version)`` in the connector layout."""
    updated = [datetime.datetime(2026, 10, 1, 8, v, tzinfo=datetime.UTC) for _, _, v in rows]
    return pa.RecordBatch.from_pydict(
        {
            "_record_id": [r for r, _, _ in rows],
            "_source": ["servicenow"] * len(rows),
            "_entity": ["incident"] * len(rows),
            "_source_key": [r.rsplit(":", 1)[1] for r, _, _ in rows],
            "_source_updated_at": pa.array(updated, _TS),
            "_fetched_at": pa.array(updated, _TS),
            "_deleted": [False] * len(rows),
            "_payload": [json.dumps({"description": d, "v": v}) for _, d, v in rows],
            "short_description": [d for _, d, _ in rows],
        }
    )


@dataclass
class PrivacyEnv:
    """The planted stores of one privacy deletion test."""

    tmp: Path
    data: Path
    enrich: PurgeEnv
    memory: MemoryStore
    vectors: VectorIndex
    cited: list[str] = field(default_factory=list)  # memory items citing RID
    kept: list[str] = field(default_factory=list)  # memory items not citing it

    # --- lake -----------------------------------------------------------------------------

    def filter(self) -> DeletionFilter:
        """A deletion filter loaded now (a sync reloads it at start and each checkpoint)."""
        filt = DeletionFilter("servicenow", "incident")
        filt.reload()
        return filt

    def sync(self, rows: list[tuple[str, str, int]], *, filt: DeletionFilter) -> int:
        """A fixture sync committing ``rows`` through ``filt``; returns the rows dropped."""
        kept, dropped = filt.apply(_lake_batch(rows))
        if kept.num_rows:
            with LakeWriter("servicenow", "incident") as writer:
                writer.write(kept)
                writer.commit()
        return dropped

    def lake_ids(self) -> list[str]:
        """``_record_id`` of every committed lake row (fresh reads)."""
        found: list[str] = []
        for part in sorted((self.data / "raw").rglob("part-*.parquet")):
            found += pq.read_table(part, columns=["_record_id"])[0].to_pylist()
        return sorted(found)

    # --- warehouse --------------------------------------------------------------------------

    def make_build(
        self, build: str, rows: list[tuple[str, str, str]], *, status: str = "promoted"
    ) -> None:
        """A build whose ``enrich.text_redacted`` holds ``(record_id, text, hash)``; a
        ``promoted`` one becomes ``CURRENT``."""
        path = warehouse.build_path(build)
        path.parent.mkdir(parents=True, exist_ok=True)
        with duckdb.connect(str(path)) as con:
            con.execute(SETTINGS_SQL.read_text("utf-8"))
            con.execute(
                "INSERT INTO meta.build (build_id, started_at, finished_at, status)"
                " VALUES (?, now(), now(), ?)",
                [build, status],
            )
            for rid, text, digest in rows:
                con.execute(
                    "INSERT INTO enrich.text_redacted VALUES (?, 'incident', ?, ?)",
                    [rid, text, digest],
                )
        if status == "promoted":
            _warehouse_rw.write_current(build)

    def build_record_ids(self, build: str) -> list[str]:
        with duckdb.connect(str(warehouse.build_path(build)), read_only=True) as con:
            rows = con.execute("SELECT record_id FROM enrich.text_redacted").fetchall()
        return sorted(str(r[0]) for r in rows)

    def complete_build(self, job_id: str) -> None:
        """Fake impl 08 completion of the rebuild: a new promoted build without the record
        becomes ``CURRENT`` and the ``build_pipeline`` job row turns ``done``."""
        self.make_build(NEW_BUILD, [(OTHER, OTHER_DESC, H_OTHER)])
        stamp = clock.format_utc(LATER)
        core.run_write(
            lambda c: c.execute(
                "UPDATE job SET status = 'done', started_at = ?, finished_at = ? WHERE job_id = ?",
                (stamp, stamp, job_id),
            ),
            op="test_complete_build",
        )

    # --- ops rows and traces ----------------------------------------------------------------

    def plant_ops_rows(self) -> None:
        sample = json.dumps(
            [{"record_id": RID, "description": DESC}, {"record_id": OTHER, "description": "x"}]
        )
        numbers = json.dumps([{"id": "n1", "row_key": {"issue": RID}, "value": 3}])
        stamp = "2026-10-01T12:00:00.000000Z"

        def op(c: Any) -> None:
            c.execute(
                "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
                " row_count, result_sample, executed_at, duration_ms)"
                " VALUES ('q_4444444444444444', NULL, ?, 'select 1', '{}', ?, 2, ?, ?, 1)",
                (OLD_BUILD, "d" * 64, sample, stamp),
            )
            c.execute(
                "INSERT INTO finding (finding_id, run_id, task_id, author_role, claim, numbers,"
                " query_ids, confidence, status, created_at) VALUES (?, 'run_x', 'task_x',"
                " 'analyst', 'Lead time [[n1]] days', ?, '[]', 0.7, 'verified', ?)",
                ("fnd_01M4H2EQZ4KZT8EPTT109D5RFB", numbers, stamp),
            )

        core.run_write(op, op="test_plant_privacy")

    def plant_traces(self) -> None:
        traces = self.data / "traces"
        traces.mkdir(parents=True, exist_ok=True)
        line = {"event": "tool_result", "rows": [{"record_id": RID, "description": DESC}]}
        (traces / "run_aaaa.jsonl").write_text(json.dumps(line) + "\n", encoding="utf-8")
        (traces / "run_bbbb.jsonl").write_text('{"event": "start"}\n', encoding="utf-8")

    def evidence_sample(self) -> str:
        return str(core.read_all("SELECT result_sample FROM evidence", ())[0][0])

    def finding_numbers(self) -> str:
        return str(core.read_all("SELECT numbers FROM finding", ())[0][0])

    def memory_ids(self) -> list[str]:
        return sorted(str(r[0]) for r in core.read_all("SELECT memory_id FROM memory_item", ()))

    def fts(self, term: str) -> list[str]:
        rows = core.read_all(
            "SELECT m.memory_id FROM memory_fts JOIN memory_item m ON m.rowid = memory_fts.rowid"
            " WHERE memory_fts MATCH ?",
            (term,),
        )
        return [str(r[0]) for r in rows]

    def request(self) -> DeletionRequest:
        found = open_deletion_request(RID)
        if found is not None:
            return found
        rows = core.read_all("SELECT request_id FROM deletion_request ORDER BY request_id", ())
        from herness.store.ops.privacy import get_deletion_request  # noqa: PLC0415

        return get_deletion_request(str(rows[-1][0]))

    def continuation(self) -> dict[str, Any]:
        """The payload of the newest queued continuation ``maintenance`` job."""
        rows: list[JobRow] = require_jobs_backend().list_jobs(
            status="queued", kind="maintenance", limit=10
        )
        found = [r for r in rows if "request_id" in r.payload]
        return dict(found[0].payload)

    def recite_during_wait(self) -> str:
        """During the rebuild wait an analyst run cites the record again (review I2): a new
        memory item, an evidence sample and a trace. Returns the new memory ID."""
        cites: dict[str, Any] = {
            **KIND_DATA["glossary"],
            "entities": [{"type": "record", "id": RID}],
        }
        again = self.memory.propose(proposal(f"Repeat {DESC} outage noted.", data=cites))
        sample = json.dumps([{"record_id": RID, "description": DESC}])
        core.run_write(
            lambda c: c.execute(
                "INSERT INTO evidence (query_id, run_id, build_id, sql, params, result_hash,"
                " row_count, result_sample, executed_at, duration_ms) VALUES"
                " ('q_5555555555555555', NULL, ?, 'select 2', '{}', ?, 1, ?, ?, 1)",
                (OLD_BUILD, "e" * 64, sample, "2026-10-02T09:10:00.000000Z"),
            ),
            op="test_recite",
        )
        line = json.dumps({"rows": [{"record_id": RID, "description": DESC}]})
        (self.data / "traces" / "run_cccc.jsonl").write_text(line + "\n", encoding="utf-8")
        return again.memory_id

    def delete_end_to_end(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        during_wait: Callable[[], object] | None = None,
        memory_purge: Callable[[str], int] | None = None,
    ) -> dict[str, Any]:
        """The IT10-06 flow: a sync whose deletion set predates step 1 commits the record
        between steps 1 and 2 and again after lake pass 1 (the late row); a sync reloaded
        after step 1 drops it; the rebuild completes (fake impl 08), ``during_wait`` runs; the
        continuation runs steps 5 to 7. Returns both outcomes and the rows each sync dropped."""
        stale = self.filter()
        real = purge_record_ids
        seen: dict[str, Any] = {}

        def lake(ids: Any) -> Any:
            if "stale" not in seen:  # a sync commit between steps 1 and 2
                seen["stale"] = self.sync([(RID, DESC, 2)], filt=stale)
                rows = [(RID, DESC, 3), (OTHER, OTHER_DESC, 3)]
                seen["fresh"] = self.sync(rows, filt=self.filter())
                seen["before_pass1"] = self.lake_ids().count(RID)
            return real(ids)

        monkeypatch.setattr(steps, "purge_record_ids", lake)
        seen["first"] = self.run(dict(BASE), memory_purge=memory_purge)
        self.complete_build(str(seen["first"].result["waiting_for"]))
        seen["wait"] = during_wait() if during_wait is not None else None
        seen["late"] = self.sync([(RID, DESC, 4)], filt=stale)  # after lake pass 1
        seen["last"] = self.run(self.continuation(), now=LATER, memory_purge=memory_purge)
        return seen

    def run(
        self,
        payload: dict[str, Any],
        *,
        now: datetime.datetime = NOW,
        memory_purge: Callable[[str], int] | None = None,
    ) -> JobOutcome:
        purge = memory_purge or (lambda rid: self.memory.purge(record_id=rid))
        return privacy.run_privacy_delete(get_config(), ctx(payload), now=now, memory_purge=purge)


def _memory_store(data: Path, redactor: Redactor) -> tuple[MemoryStore, VectorIndex]:
    """The real MemoryStore on the migrated ops store; vectors in ``data/vectors``."""
    vectors = VectorIndex()
    store = MemoryStore(
        MemoryConfig(injection_patterns=PATTERNS), conn_factory=core.connection,
        vectors=vectors, embedder=Embedder(FakeEmbed(), model_name="bge-m3"),
        redactor=redactor, llms=None, allowed_numeral_patterns=(r"(INC|CHG|PRB)\d+",),
        data_root=data,
    )  # fmt: skip
    return store, vectors


@pytest.fixture
def privacy_env(
    tmp_path: Path, reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> Iterator[PrivacyEnv]:
    """Every store of IT10-06 planted with the record ``RID`` and the kept record ``OTHER``;
    the test redactor (fixed key) serves both memory and the job payload scan (U08-45)."""
    del reset_process_state
    enrich = purge_env(tmp_path)
    data = data_layout().root
    directory = NameDirectory.from_files(None, (), None)
    redactor = Redactor(RedactionConfig(directory_file=None), bytes(range(32)), directory)
    monkeypatch.setattr(redact._State, "redactor", redactor)
    reset_connections(path=data / "ops.sqlite")
    try:
        migrate()
        bind_core_backends()
        memory, vectors = _memory_store(data, redactor)
        env = PrivacyEnv(tmp_path, data, enrich, memory, vectors)
        env.sync([(RID, DESC, 1), (OTHER, OTHER_DESC, 1)], filt=env.filter())
        enrich.add_vectors([(RID, H_RID), (OTHER, H_OTHER)])
        enrich.cache([H_RID, H_OTHER])
        enrich.labels("teacher", [(RID, H_RID, "q_a"), (OTHER, H_OTHER, "q_a")])
        enrich.labels("human", [(RID, H_RID, "q_a")])
        env.make_build(OLD_BUILD, [(RID, DESC, H_RID), (OTHER, OTHER_DESC, H_OTHER)])
        cites: dict[str, Any] = {
            **KIND_DATA["glossary"],
            "entities": [{"type": "record", "id": RID}],
        }
        active = memory.propose(proposal(f"Churn spiked after the {DESC} outage.", data=cites))
        pending = memory.propose(
            proposal(f"Backlog means open {DESC} tickets.", provenance("agent"), data=cites)
        )
        kept = memory.propose(proposal("MTTR means the mean time to restore a service."))
        env.cited, env.kept = [active.memory_id, pending.memory_id], [kept.memory_id]
        env.plant_ops_rows()
        env.plant_traces()
        core.connection().execute("PRAGMA wal_checkpoint(TRUNCATE)")  # rows in the main file
        yield env
    finally:
        reset_connections()
        reset_config()
