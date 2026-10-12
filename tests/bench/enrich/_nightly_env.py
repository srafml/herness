"""The nightly enrichment scenario of BT03-06 and BT03-10 (T03-36): two consecutive nights.

Real: `herness.enrich.pipeline.run_enrichment` with every stage (text with the real redaction,
embed into LanceDB, decide-primary, decide-escalate over HTTP, cluster, reasoning, link,
suggest, resolve), the decision cache, the label and review stores. Faked, as the spec allows
for the CPU-share parts ("model stages via the test fakes/stubs"): the bge-m3 encoder (a
deterministic word-sum encoder, `HashEncoder`), the Laya student (`BenchLayaAgent`, an accepted
`CURRENT` version whose manifest accepts every question), OpenJev (`StubDeciderServer`, hash
mode, over loopback HTTP) and the LLM (`FakeLLMClient`). The real-model figure of the nightly
total is owed on the target PC.

Scenario (both nights on copies of the lake's `core.*` build, pinned clock):

* night 1 (the priming, "yesterday"): the build minus 5,000 records (4,000 incidents, 750
  changes, 250 problems), enriched from nothing, so every stage has a complete previous state:
  texts, vectors, cached decisions, a cluster snapshot;
* night 2 (the measured one): the full build, previous warehouse = night 1. 5,000 records are
  new (the ones removed above) and 5,000 others are changed (description and
  `source_updated_at` edited), 10,000 in all, split 8,000 incidents, 1,500 changes, 500
  problems. No recluster is forced and none is due (the snapshot is minutes old), nothing is
  left over from night 1 (no backlog: night 1 escalates below the nightly cap).

The student is confident on all but `LOW_RATE` of the texts (a fixed share, chosen by a hash of
the text); those fall below the gate and escalate to the stub teacher.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import random
import re
import shutil
import sys
import time
import zlib
from collections.abc import Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Final, Literal, cast

import duckdb
import keyring
import numpy as np
import pytest
import yaml
from tests.bench.enrich._lake import LakeBuild, activate_enrich
from tests.integration.enrich._pipeline_env import QSV, MergingContext
from tests.support._build_clock import pinned_clock
from tests.support.fake_keyring import MemoryKeyring
from tests.support.fake_laya import FakeLayaAgent, fake_laya_module, write_laya_version
from tests.support.stub_decider import StubDeciderServer
from tests.unit.enrich._fake_llm import FakeLLMClient

from herness.core import config as herness_config
from herness.core.resilience import bind_ops_backend, reset_process_state
from herness.core.types import GpuClass, LLMRequest, ServiceName
from herness.enrich import _pipeline_stages, decide_stage, embed
from herness.enrich.embed import Encoder
from herness.enrich.pipeline import EnrichReport, run_enrichment
from herness.store import warehouse
from herness.store.ops import reset_connections
from herness.store.ops.resilience import SqliteResilienceBackend

__all__ = ["LOW_RATE", "Night", "run_two_nights"]

LAYA_VERSION: Final = "laya-20260901-1"
LOW_RATE: Final = 0.02  # share of texts the fake student is unsure about
CONFIDENT: Final = 0.97
SPLIT: Final = (("incident", "core.incident", 4_000), ("change", "core.change", 750),
                ("problem", "core.problem", 250))  # fmt: skip
NIGHT1: Final = "20260831-000000-BENCH1"
NIGHT2: Final = "20260901-000000-BENCH2"
_REDACT_KEY: Final = bytes(range(32))
_WORD: Final = re.compile(r"[a-z]+")


class HashEncoder(Encoder):
    """Deterministic CPU stand-in for bge-m3: the unit sum of fixed per-word random vectors,
    so texts sharing words are close and the cluster stage sees structure."""

    def __init__(self) -> None:
        super().__init__(Path("bench-hash"), model_name="bench/hash")
        self._words: dict[str, int] = {}
        self._matrix = np.zeros((0, 1024), dtype=np.float32)

    def load(self, device: Any) -> None:
        return None

    def unload(self) -> None:
        return None

    def _row(self, word: str) -> int:
        if word not in self._words:
            rng = np.random.default_rng(zlib.crc32(word.encode()))
            vec = rng.standard_normal((1, 1024)).astype(np.float32)
            self._matrix = np.concatenate([self._matrix, vec])
            self._words[word] = len(self._words)
        return self._words[word]

    def encode(self, texts: Sequence[str], *, batch_size: int) -> np.ndarray:
        out = np.zeros((len(texts), 1024), dtype=np.float32)
        for i, text in enumerate(texts):
            rows = [self._row(w) for w in _WORD.findall(text.lower())] or [self._row("empty")]
            out[i] = self._matrix[rows].sum(axis=0)
        norm = np.linalg.norm(out, axis=1, keepdims=True)
        return cast("np.ndarray", out / np.maximum(norm, 1e-9))


def _answer(spec: Mapping[str, object], *, confident: bool) -> dict[str, object]:
    """One Jev-wire answer: probability `CONFIDENT` on the first label, or uniform."""
    kind = spec["type"]
    if kind == "noul":
        return {"noul": CONFIDENT if confident else 0.5}
    criteria = spec["criteria"]
    labels = list(criteria) if isinstance(criteria, Mapping) else ["0", "1", "2", "3"]
    k = len(labels)
    top = CONFIDENT if confident else 1.0 / k
    rest = (1.0 - top) / (k - 1)
    probs = {label: (top if i == 0 else rest) for i, label in enumerate(labels)}
    answer: dict[str, object] = {"confidence": top, "probabilities": probs}
    if kind == "choice":
        answer["choice"] = labels[0]
    return answer


class BenchLayaAgent(FakeLayaAgent):
    """An accepted fake student: confident, except on a `low_rate` share of the texts (by
    a hash of the text, so a text is always sure or always unsure)."""

    def __init__(self) -> None:
        super().__init__()
        self.low_rate = 0.0

    def _confident(self, state: str) -> bool:
        bucket = int.from_bytes(hashlib.sha256(state.encode()).digest()[:4], "big") % 10_000
        return bucket >= self.low_rate * 10_000

    def _result(self, state: str, questions: Mapping[str, Mapping[str, object]]) -> object:
        confident = self._confident(state)
        return {"answers": {q: _answer(spec, confident=confident) for q, spec in questions.items()}}

    def predict_shortlist(
        self, state: str, questions: Sequence[Any], *, embed_fn: Any, k: int = 16
    ) -> dict[str, object]:
        """The wide-choice path (more than 20 options): the same confidence rule over the
        first `k` labels; `embed_fn` is not called (the real agent embeds the labels)."""
        confident = self._confident(state)
        answers: dict[str, object] = {}
        for wire in questions:
            for qid, spec in wire.items():
                labels = list(spec["criteria"])
                top = labels[:k]
                first = CONFIDENT if confident else 1.0 / len(top)
                rest = (1.0 - first) / max(len(top) - 1, 1)
                probs = {x: (first if x == top[0] else rest if x in top else 0.0) for x in labels}
                answers[qid] = {"choice": top[0], "probabilities": probs, "confidence": first}
        return {"answers": answers}


def _vote(req: LLMRequest) -> dict[str, object]:
    """Every vote picks each question's first label."""
    schema = cast("dict[str, Any]", req.response_schema or {})
    props: dict[str, Any] = schema.get("properties", {})
    return {qid: {"answer": spec["properties"]["answer"]["enum"][0]} for qid, spec in props.items()}


@dataclasses.dataclass
class _GpuView:
    ctx: MergingContext | None = None

    def loaded_class(self) -> GpuClass:
        return "none" if self.ctx is None else self.ctx.current_class

    def service_healthy(self, name: ServiceName) -> bool:
        return self.ctx is not None and name in self.ctx.services.running


@dataclasses.dataclass(frozen=True)
class Night:
    """The two nights' reports and the measured wall time of night 2."""

    report1: EnrichReport
    report2: EnrichReport
    night1_s: float
    night2_s: float
    incidents: int
    new_or_changed: int
    openjev_calls: int
    decisions: int
    escalated_decisions: int
    night_pairs: int  # (record, question) decisions of the night's new and changed records
    night_escalated_pairs: int
    night_new_incident_hashes: int  # distinct incident texts of the night absent from night 1


def _shipped(lake: LakeBuild, name: str) -> dict[str, Any]:
    return cast("dict[str, Any]", yaml.safe_load((lake.env.cfg_dir / name).read_text("utf-8")))


def _config_edits(lake: LakeBuild, stub_url: str) -> tuple[dict[str, Any], dict[str, Any]]:
    decisions = _shipped(lake, "decisions.yaml")
    decisions["question_set_version"] = QSV
    decisions["change_link"]["use_decider"] = False  # pair decisions are a model step
    decisions["escalation"]["bootstrap_window_days"] = 3650  # the 2024-26 lake is in scope
    models = _shipped(lake, "models.yaml")
    models["deciders"]["openjev"]["base_url"] = stub_url
    models["deciders"]["openjev"]["concurrency"] = 4  # the stub's listen backlog is 5
    return ({k: decisions[k] for k in ("question_set_version", "change_link", "escalation")},
            {"deciders": models["deciders"]})  # fmt: skip


def _write_laya(ndata: Path, question_ids: Sequence[str]) -> None:
    """An accepted Laya version whose manifest accepts every question of the set."""
    root = ndata / "models" / "laya"
    directory = write_laya_version(root, LAYA_VERSION)
    calibration = {"decider": "laya", "decider_version": LAYA_VERSION,
                   "question_set_version": QSV, "fitted_at": "2026-09-01T00:00:00+00:00",
                   "questions": {}}  # fmt: skip
    (directory / "calibration.json").write_text(json.dumps(calibration), encoding="utf-8")
    manifest = directory / "manifest.json"
    doc = json.loads(manifest.read_text("utf-8"))
    doc["accepted_questions"] = list(question_ids)
    manifest.write_text(json.dumps(doc), encoding="utf-8")
    (root / "CURRENT").write_text(LAYA_VERSION, encoding="utf-8")


def _no_sleep(_seconds: float) -> None:
    return None


async def _no_asleep(_seconds: float) -> None:
    return None


def _bind_resilience() -> None:
    """A fresh process state on the ops store of the row, with the retry sleeps removed (as
    the `reset_process_state` fixture does): breakers and retries of the deciders work."""
    state = reset_process_state()
    state.rng = random.Random(0)
    state.sleep = _no_sleep
    state.asleep = _no_asleep
    bind_ops_backend(SqliteResilienceBackend())


def _copy_build(lake: LakeBuild, build_id: str) -> duckdb.DuckDBPyConnection:
    """A writable copy of the lake build as a build file of its own."""
    dst = warehouse.build_path(build_id, layout=lake.env.layout)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(lake.path, dst)
    return lake.open_id(build_id)


def _split_ids(con: duckdb.DuckDBPyConnection) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """Deterministic (new, changed) record ids per core table, by `sha256(record_id)` order."""
    new: dict[str, list[str]] = {}
    changed: dict[str, list[str]] = {}
    for _, table, n in SPLIT:
        rows = con.execute(
            f"SELECT record_id FROM {table} WHERE record_id IS NOT NULL"  # noqa: S608
            f" AND source_updated_at IS NOT NULL ORDER BY sha256(record_id) LIMIT {2 * n}"
        ).fetchall()
        ids = [r[0] for r in rows]
        new[table], changed[table] = ids[:n], ids[n:]
    return new, changed


def _ids_table(con: duckdb.DuckDBPyConnection, ids: list[str]) -> None:
    con.execute("CREATE OR REPLACE TEMP TABLE bench_ids (record_id VARCHAR)")
    con.executemany("INSERT INTO bench_ids VALUES (?)", [[i] for i in ids])


def _new_incident_hashes(con: duckdb.DuckDBPyConnection, prev: Path) -> tuple[int] | None:
    """Distinct incident texts of the `bench_ids` records that night 1 did not have."""
    con.execute(f"ATTACH '{prev.as_posix()}' AS p1 (READ_ONLY)")
    try:
        return cast("tuple[int] | None", con.execute(
            "SELECT count(DISTINCT t.content_hash) FROM enrich.text_redacted AS t"
            " WHERE t.entity = 'incident' AND t.record_id IN (SELECT record_id FROM bench_ids)"
            " AND t.content_hash NOT IN (SELECT content_hash FROM p1.enrich.text_redacted)"
        ).fetchone())  # fmt: skip
    finally:
        con.execute("DETACH p1")


def _edit_changed(con: duckdb.DuckDBPyConnection, changed: dict[str, list[str]]) -> None:
    for _, table, _ in SPLIT:
        _ids_table(con, changed[table])
        body = "root_cause_text" if table == "core.problem" else "description"
        con.execute(
            f"UPDATE {table} SET {body} = coalesce({body}, '') || ' Follow-up: vendor engaged.',"  # noqa: S608
            f" source_updated_at = source_updated_at + INTERVAL 1 DAY"
            f" WHERE record_id IN (SELECT record_id FROM bench_ids)"
        )


def _drop_new(con: duckdb.DuckDBPyConnection, new: dict[str, list[str]]) -> None:
    for _, table, _ in SPLIT:
        _ids_table(con, new[table])
        con.execute(f"DELETE FROM {table} WHERE record_id IN (SELECT record_id FROM bench_ids)")  # noqa: S608


def _run(con: duckdb.DuckDBPyConnection, build_id: str, ctx: MergingContext, prev: Path | None,
         factory: Any) -> tuple[EnrichReport, float]:  # fmt: skip
    start = time.perf_counter()
    report = run_enrichment(con, build_id, depth="standard", ctx=ctx, prev_warehouse=prev,
                            llm_factory=factory)  # fmt: skip
    return report, time.perf_counter() - start


def run_two_nights(lake: LakeBuild, work: Path) -> Iterator[Night]:  # noqa: PLR0915 - the scenario
    """Prime night 1, measure night 2, yield the result, then undo every patch and file."""
    ndata = work / "night-data"
    previous_keyring = keyring.get_keyring()
    keyring.set_keyring(backend := MemoryKeyring())
    backend.store[("herness", "redact.hmac_key")] = _REDACT_KEY.hex()
    agent, encoder, gpu, llm = BenchLayaAgent(), HashEncoder(), _GpuView(), FakeLLMClient(_vote)

    def factory(role: Literal["enrich_decider", "cluster_namer"]) -> tuple[Any, str, int]:
        return llm, "local-30b/qwen3-bench", 8

    try:
        with StubDeciderServer("hash") as stub, pytest.MonkeyPatch.context() as mp, pinned_clock():
            decisions, models = _config_edits(lake, stub.base_url)
            cfg = activate_enrich(lake, ndata, "night", decisions=decisions, models=models)
            _bind_resilience()
            _write_laya(ndata, [q.id for q in cfg.decisions.questions])
            mp.setitem(sys.modules, "laya", fake_laya_module(agent))
            mp.setattr(_pipeline_stages, "get_encoder", lambda: encoder)
            mp.setattr(_pipeline_stages, "device", lambda: "cpu")
            mp.setattr(embed, "release_cuda", lambda: None)
            mp.setattr(decide_stage, "gpu_state", lambda: gpu)
            con1 = _copy_build(lake, NIGHT1)
            new, changed = _split_ids(con1)
            _drop_new(con1, new)
            ctx1 = MergingContext({"stages": ["enrich"], "depth": "standard"})
            gpu.ctx = ctx1
            agent.low_rate = LOW_RATE
            report1, night1_s = _run(con1, NIGHT1, ctx1, None, factory)
            con1.execute("CHECKPOINT")
            con1.close()
            con2 = _copy_build(lake, NIGHT2)
            _edit_changed(con2, changed)
            ctx2 = MergingContext({"stages": ["enrich"], "depth": "standard"})
            gpu.ctx = ctx2
            calls_before = stub.calls
            prev = warehouse.build_path(NIGHT1, layout=lake.env.layout)
            report2, night2_s = _run(con2, NIGHT2, ctx2, prev, factory)
            row = con2.execute("SELECT count(*), count(*) FILTER (WHERE escalated)"
                               " FROM enrich.decision").fetchone()  # fmt: skip
            incidents = con2.execute("SELECT count(*) FROM core.incident").fetchone()
            _ids_table(con2, [i for part in (new, changed) for ids in part.values() for i in ids])
            fresh = _new_incident_hashes(con2, prev)
            mine = con2.execute(
                "SELECT count(*), count(*) FILTER (WHERE escalated) FROM enrich.decision"
                " WHERE record_id IN (SELECT record_id FROM bench_ids)"
            ).fetchone()
            con2.close()
            yield Night(report1, report2, night1_s, night2_s, int(incidents[0] if incidents else 0),
                        sum(n for _, _, n in SPLIT) * 2, stub.calls - calls_before,
                        int(row[0]) if row else 0, int(row[1]) if row else 0,
                        int(mine[0]) if mine else 0, int(mine[1]) if mine else 0,
                        int(fresh[0]) if fresh else 0)  # fmt: skip
    finally:
        reset_process_state()
        reset_connections()
        herness_config.reset_config()
        keyring.set_keyring(previous_keyring)
        shutil.rmtree(ndata, ignore_errors=True)
        for build_id in (NIGHT1, NIGHT2):
            path = warehouse.build_path(build_id, layout=lake.env.layout)
            path.unlink(missing_ok=True)
            path.with_name(path.name + ".wal").unlink(missing_ok=True)
