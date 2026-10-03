"""Private sibling of `distill` (T03-32 spec note): the data steps of one distillation round.

Config and gold refresh, the stop rule, teacher selection and labeling, teacher rows,
spot-checks and blocking, active scoring, training with the candidate manifest and the
candidate's gold inference (flows F03-13 and F03-14). Gold hashes and the hashes of pending
gold items never reach a sample, a teacher row or a training set (TH03-04). Logs and errors
carry ids, hashes and counts only, never ticket text (TH03-03).
"""

from __future__ import annotations

import contextlib
import dataclasses
import hashlib
import json
from collections.abc import Callable, Iterator, Mapping, Sequence
from itertools import pairwise
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal

import duckdb
import numpy as np
import pyarrow as pa

from herness.core import time as clock
from herness.core.config import get_config
from herness.core.errors import ConfigError, SchemaViolation, StoreBusy
from herness.core.logging import get_logger
from herness.core.types import DecisionInput, QuestionSet
from herness.enrich._cluster_io import ClusterSnapshot, matrix
from herness.enrich.cache import DecisionCache, replace_atomic
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.embed_stage import FILTER_MAX, lance_filter_in
from herness.enrich.evaluate import _cache_rows
from herness.enrich.gold import consolidate_gold, request_gold
from herness.enrich.gpu import YieldRequested
from herness.enrich.labels import TEACHER_SCHEMA, LabelStore, sync_label_checks
from herness.enrich.laya_models import LayaManifest, _read_manifest, new_version_id, read_current
from herness.enrich.laya_trainer import TrainHyper, build_training_set, select_trainer
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import PAIR_QUESTIONS, check_fingerprint_registry, load_question_set
from herness.enrich.review_items import iter_review_items
from herness.enrich.sampling import _nearest, select_active, stratified_sample
from herness.enrich.settings import check_decider_refs
from herness.store.vectors import EMBEDDING_DIM, VectorStore
from herness.store.warehouse import open_readonly

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.jobs import JobContext
    from herness.enrich.decide import Decider

type RoundKind = Literal["initial", "active"]
type StopReason = Literal["none", "min_gain", "max_rounds"]
type TeacherName = Literal["openjev", "llm"]

SAMPLES: Final = 3  # OpenJev `samples` and LLM votes for teacher labels (design 03 §5.8)
CHUNK: Final = 2_000  # inputs per `decide` call
FLUSH_ROWS: Final = 2_000  # cache writer flush every 2,000 answers
BASE_DIR: Final = "base"  # data/models/laya/base: the published Laya checkpoint (spec note)
BASE_CHECKPOINT: Final = "convaiinnovations/laya"
_HASHES: Final = "_distill_hashes"  # registered Arrow view (fixed name)
_RECORD_COLS: Final = "min(record_id) AS record_id, arg_min(entity, record_id) AS entity, " \
    "content_hash, arg_min(text, record_id) AS text"  # fmt: skip
_RECORDS_SQL: Final = f"""SELECT {_RECORD_COLS} FROM enrich.text_redacted
WHERE content_hash IN (SELECT content_hash FROM {_HASHES})
GROUP BY content_hash ORDER BY content_hash"""  # noqa: S608 - fixed names only
_POOL_SQL: Final = f"""SELECT {_RECORD_COLS} FROM enrich.text_redacted
WHERE list_contains($entities, entity)
  AND content_hash NOT IN (SELECT content_hash FROM {_HASHES})
GROUP BY content_hash ORDER BY content_hash LIMIT $pool"""  # noqa: S608 - fixed names only

_log = get_logger("enrich.distill")


@dataclasses.dataclass(frozen=True)
class Run:
    """What every step of one round reads: config, paths, question sets and stores."""

    cfg: HernessConfig
    round_kind: RoundKind
    paths: EnrichPaths
    all_qs: QuestionSet  # the whole set (label sync)
    qs: QuestionSet  # without pair questions: what the teacher and Laya are asked
    store: LabelStore
    cache: DecisionCache


def load_run(round_kind: RoundKind) -> Run:
    """Step 0 (config part): config, `check_decider_refs`, question set, fingerprint registry.
    ConfigError before any GPU work."""
    cfg = get_config()
    issues = check_decider_refs(cfg.decisions, cfg.models.deciders)
    errors = sorted(issue["path"] for issue in issues if issue["severity"] == "error")
    if errors:
        msg = "distill: decisions name a disabled decider"
        raise ConfigError(msg, details={"paths": ",".join(errors)})
    qs = load_question_set(cfg.decisions)
    paths = EnrichPaths.from_config(cfg)
    check_fingerprint_registry(qs, paths=paths)
    asked = tuple(q for q in qs.questions if q.id not in PAIR_QUESTIONS)
    return Run(cfg, round_kind, paths, qs, QuestionSet(version=qs.version, questions=asked),
               LabelStore(paths, qs.version), DecisionCache(paths, qs.version))  # fmt: skip


@contextlib.contextmanager
def warehouse() -> Iterator[duckdb.DuckDBPyConnection]:
    """The promoted warehouse, read-only, closed on exit."""
    with contextlib.closing(open_readonly(None)) as wh:
        yield wh


def refresh_gold(run: Run) -> list[str]:
    """Step 0 (labels part, F03-18): sync decided items, consolidate gold; frozen questions."""
    sync_label_checks(run.store, qs=run.all_qs)
    status = consolidate_gold(run.store, qs=run.all_qs, cfg=run.cfg.decisions, now=clock.now())
    return sorted(qid for qid, gold in status.items() if gold.frozen)


def gold_exclusion(run: Run) -> frozenset[str]:
    """Every gold hash plus the hashes of pending gold items (TH03-04)."""
    match = {"question_set_version": run.qs.version, "purpose": "gold"}
    items = iter_review_items("label_check", "pending", payload_match=match)
    pending = {str(item.payload.get("content_hash")) for item in items}
    return frozenset(run.store.gold_hashes() | pending)


def _macro(directory: Path) -> float | None:
    try:
        value = json.loads((directory / "eval.json").read_text("utf-8")).get("macro_metric")
    except (OSError, ValueError, AttributeError):
        return None
    return float(value) if isinstance(value, int | float) else None


def plan_round(run: Run) -> tuple[int, str | None, StopReason]:
    """(round, parent version, stop reason); the active stop rule reads the chain from CURRENT."""
    current = read_current(run.paths) if run.paths.laya_current().is_file() else None
    if run.round_kind == "initial":
        return 0, current if run.cfg.decisions.distill.init_from == "previous" else None, "none"
    active = run.cfg.decisions.distill.active
    chain: list[tuple[int, str, float | None]] = []
    version: str | None = read_current(run.paths)  # ConfigError without an accepted version
    while version is not None and len(chain) <= active.max_rounds:
        manifest = _read_manifest(run.paths.laya_dir(version), version)
        params = manifest.hyperparams
        chain.append((int(params["round"]), str(params["round_kind"]),
                      _macro(run.paths.laya_dir(version))))  # fmt: skip
        version = manifest.parent_version
    head = chain[0][0]
    if head >= active.max_rounds:
        return head + 1, current, "max_rounds"
    gains = [(new[2] - old[2]) * 100 for new, old in pairwise(chain)
             if new[1] == "active" and new[2] is not None and old[2] is not None]  # fmt: skip
    recent = gains[: active.patience]
    small = len(recent) == active.patience and all(g < active.min_gain_pp for g in recent)
    return head + 1, current, "min_gain" if small else "none"


def create_version(run: Run) -> str:
    """Step 2: a new version id and its directory (``exist_ok=False``; collision StoreBusy)."""
    version = new_version_id(run.paths, now=clock.now())
    try:
        run.paths.laya_dir(version).mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        msg = "laya version directory exists"
        raise StoreBusy(msg, version=version) from exc
    return version


def query(wh: duckdb.DuckDBPyConnection, sql: str, hashes: Sequence[str],
          params: Mapping[str, object] | None = None) -> pa.Table:  # fmt: skip
    """Run a fixed query with ``hashes`` registered as ``_distill_hashes``."""
    wh.register(_HASHES, pa.table({"content_hash": pa.array(sorted(hashes), pa.string())}))
    try:
        return wh.execute(sql, dict(params or {})).to_arrow_table()
    except duckdb.Error as exc:
        msg = f"distill query failed: {type(exc).__name__}"
        raise SchemaViolation(msg) from exc
    finally:
        wh.unregister(_HASHES)


def _inputs(records: pa.Table) -> list[DecisionInput]:
    names = ("record_id", "entity", "content_hash", "text")
    rows = zip(*(records.column(n).to_pylist() for n in names), strict=True)
    return [DecisionInput(record_id=r, entity=e, content_hash=h, text=t) for r, e, h, t in rows]


def label(run: Run, ctx: JobContext, decider: Decider, records: pa.Table, *,
          samples: int | None, stage: str) -> None:  # fmt: skip
    """Decide ``records`` in chunks into the cache, skipping cached keys; a yield request
    flushes the writer, then raises ``YieldRequested(stage)``."""
    cached = run.cache.existing_keys(decider.name, decider.version, run.qs)
    todo = [item for item in _inputs(records)
            if any((item.content_hash, q.id) not in cached
                   for q in run.qs.for_entity(item.entity).questions)]  # fmt: skip
    writer = run.cache.writer(decider.name, decider.version, questions=run.qs,
                              flush_rows=FLUSH_ROWS)  # fmt: skip
    with writer:
        for start in range(0, len(todo), CHUNK):
            if ctx.should_yield():
                writer.flush()
                raise YieldRequested(stage)
            writer.add(decider.decide(todo[start : start + CHUNK], run.qs), samples=samples)
            ctx.heartbeat(stage)


def append_teacher_rows(
    run: Run, teacher: tuple[str, str], sample: pa.Table, round_no: int
) -> pa.Table:
    """Step 5: this round's teacher rows from the cache (idempotent); returns them all."""
    dists = _cache_rows(run.cache, teacher, run.qs)
    table = run.store.read("teacher")
    mine = [r for r in table.to_pylist() if r["round"] == round_no and r["decider"] == teacher[0]]
    have = {(r["content_hash"], r["question"]) for r in mine}
    rows = []
    for rec in sample.to_pylist():
        for q in run.qs.for_entity(rec["entity"]).questions:
            dist, key = dists.get((q.id, rec["content_hash"])), (rec["content_hash"], q.id)
            if dist and key not in have:
                rows.append({
                    "content_hash": key[0], "record_id": rec["record_id"], "question": q.id,
                    "question_fingerprint": q.fingerprint, "decider": teacher[0],
                    "answer": max(dist, key=dist.__getitem__), "distribution": sorted(dist.items()),
                    "decider_version": teacher[1], "round": round_no,
                    "stratum": rec.get("stratum"), "purpose": run.round_kind,
                })  # fmt: skip
    new = pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA)
    run.store.append("teacher", new)
    return pa.concat_tables([pa.Table.from_pylist(mine, schema=TEACHER_SCHEMA), new])


def latest_teacher(store: LabelStore) -> dict[tuple[str, str, str], str]:
    """Latest-round teacher answer per (content_hash, question, fingerprint)."""
    rows = store.read("teacher", columns=["content_hash", "question", "question_fingerprint",
                                          "answer", "round"]).to_pylist()  # fmt: skip
    rows.sort(key=lambda r: -1 if r["round"] is None else r["round"])
    return {
        (r["content_hash"], r["question"], r["question_fingerprint"]): r["answer"] for r in rows
    }


def vector_reader(paths: EnrichPaths) -> Callable[[Sequence[str]], np.ndarray]:
    """Stored ``ticket_embedding`` vectors by content hash (zeros when absent)."""

    def read(hashes: Sequence[str]) -> np.ndarray:
        table = VectorStore(paths.vectors_dir()).table("ticket_embedding")
        out = np.zeros((len(hashes), EMBEDDING_DIM), dtype=np.float32)
        index = {h: i for i, h in enumerate(hashes)}
        for start in range(0, len(hashes), FILTER_MAX):
            where = lance_filter_in("content_hash", list(hashes[start : start + FILTER_MAX]))
            got = table.search().where(where).select(["content_hash", "vector"]).limit(None)
            found = got.to_arrow()
            vectors = matrix(found.column("vector"), EMBEDDING_DIM)
            for h, vector in zip(found.column("content_hash").to_pylist(), vectors, strict=True):
                out[index[h]] = vector
        return out

    return read


def request_gold_items(run: Run, wh: duckdb.DuckDBPyConnection) -> int:
    """Step 7: `request_gold` for unfrozen questions; items created."""
    answers = latest_teacher(run.store)
    fps = {q.id: q.fingerprint for q in run.qs.questions}
    created = request_gold(wh, qs=run.qs, store=run.store, cfg=run.cfg.decisions,
                           teacher_answers=lambda h, qid: answers.get((h, qid, fps[qid] or "")),
                           snapshot=ClusterSnapshot.load_current(run.paths),
                           vector_reader=vector_reader(run.paths))  # fmt: skip
    return sum(created.values())


def initial_sample(run: Run, wh: duckdb.DuckDBPyConnection, teacher: TeacherName,
                   excluded: frozenset[str], version: str) -> pa.Table:  # fmt: skip
    """Step 4 (initial): the stratified sample, gold excluded."""
    settings = run.cfg.decisions.distill
    size = settings.sample_size if teacher == "openjev" else settings.sample_size_llm_teacher
    return stratified_sample(wh, qs=run.qs, size=size, exclude_hashes=excluded,
                             salt=f"distill:{version}",
                             snapshot=ClusterSnapshot.load_current(run.paths),
                             vector_reader=vector_reader(run.paths))  # fmt: skip


def _uncertainty(run: Run, ctx: JobContext, laya: LayaDecider, pool: pa.Table) -> np.ndarray:
    """``u = max over scoring questions of (1 - p'_max)`` with CURRENT's temperatures."""
    calibration, scoring = (
        CalibrationStore(run.paths),
        [q.id for q in run.qs.questions if q.scoring_use],
    )
    temps = {qid: calibration.temperature("laya", laya.version, run.qs.version, qid)[0]
             for qid in scoring}  # fmt: skip
    items, u = _inputs(pool), np.zeros(pool.num_rows)
    for start in range(0, len(items), CHUNK):
        if ctx.should_yield():
            stage = "active_scoring"
            raise YieldRequested(stage)
        for offset, out in enumerate(laya.decide(items[start : start + CHUNK], run.qs)):
            for qid in scoring:
                answer = out.answers.get(qid)
                if answer is not None:
                    logits = np.log(np.asarray(list(answer.distribution.values())) + 1e-12)
                    p = np.exp((logits - logits.max()) / temps[qid])
                    u[start + offset] = max(u[start + offset], 1.0 - float(p.max() / p.sum()))
        ctx.heartbeat("active_scoring")
    return u


def active_ranked(run: Run, ctx: JobContext, wh: duckdb.DuckDBPyConnection,
                  excluded: frozenset[str]) -> pa.Table:  # fmt: skip
    """Step 4 (active, before the teacher starts): CURRENT scores the pool of records without
    teacher rows (hash order); `select_active` up to the larger per-round size."""
    active = run.cfg.decisions.distill.active
    teacher = set(run.store.read("teacher", columns=["content_hash"]).column(0).to_pylist())
    entities = sorted({e for q in run.qs.questions for e in q.applies_to} & {"incident", "problem"})
    pool = query(
        wh, _POOL_SQL, sorted(excluded | teacher), {"entities": entities, "pool": active.pool}
    )
    laya = LayaDecider(
        run.cfg.models.deciders.laya, paths=run.paths, version=read_current(run.paths)
    )
    try:
        u = _uncertainty(run, ctx, laya, pool)
    finally:
        laya.unload()
    hashes = pool.column("content_hash").to_pylist()
    snapshot = ClusterSnapshot.load_current(run.paths)
    protos = (np.arange(len(hashes)) if snapshot is None or not hashes
              else _nearest(hashes, snapshot, vector_reader(run.paths)))  # fmt: skip
    most = max(active.per_round, active.per_round_llm_teacher)
    picked = select_active(u, protos, hashes, candidates=active.candidates,
                           per_prototype=active.per_prototype, per_round=most)  # fmt: skip
    return pool.take(pa.array(picked, pa.int64()))


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while block := handle.read(1 << 23):
            digest.update(block)
    return digest.hexdigest()


def train_candidate(run: Run, ctx: JobContext, wh: duckdb.DuckDBPyConnection,
                    plan: Mapping[str, Any]) -> tuple[int, int]:  # fmt: skip
    """Steps 9-10: training set (gold excluded), train, manifest ``status = "candidate"``.
    ``plan`` holds version, parent, round, teacher, teacher_version and blocked."""
    teacher, gold = run.store.read("teacher"), gold_exclusion(run)
    hashes = sorted(set(teacher.column("content_hash").to_pylist()) - gold)
    records = query(wh, _RECORDS_SQL, hashes)
    texts = dict(zip(records.column("content_hash").to_pylist(),
                     records.column("text").to_pylist(), strict=True))  # fmt: skip
    trainable = tuple(q for q in run.qs.questions if q.id not in plan["blocked"])
    questions = QuestionSet(version=run.qs.version, questions=trainable)
    data = build_training_set(teacher, run.store.latest_human(), texts=texts, gold=gold,
                              questions=questions)  # fmt: skip
    parent, out = plan["parent"], run.paths.laya_dir(plan["version"])
    init_dir = run.paths.laya_dir(parent) if parent else run.paths.laya_root() / BASE_DIR
    trainer, hyper = select_trainer(), TrainHyper()
    result = trainer.train(data, init_dir=init_dir, out_dir=out, hyper=hyper, ctx=ctx)
    manifest = LayaManifest(
        version=plan["version"], parent_version=parent,
        base_checkpoint=parent or BASE_CHECKPOINT, teacher=plan["teacher"],
        teacher_version=plan["teacher_version"], question_set_version=run.qs.version,
        train_data_sha256=data.sha256, n_train=data.train.num_rows,
        hyperparams={"trainer": trainer.name, "seed": hyper.seed, "round": plan["round"],
                     "round_kind": run.round_kind, "epochs_run": result.epochs_run},
        weights_sha256={p.relative_to(out).as_posix(): _file_sha256(p) for p in result.files},
        accepted_questions=[], status="candidate", created_at=clock.now(),
        accepted_by=None, accepted_at=None,
    )  # fmt: skip
    payload = manifest.model_dump_json().encode("utf-8")
    replace_atomic(out / "manifest.json", lambda tmp: tmp.write_bytes(payload), kind="manifest")
    return data.train.num_rows, data.val.num_rows


def infer_gold(run: Run, ctx: JobContext, wh: duckdb.DuckDBPyConnection, version: str) -> None:
    """Step 11: the candidate decides every gold hash into its own cache partition."""
    hashes = sorted(run.store.gold_hashes())
    laya = LayaDecider(run.cfg.models.deciders.laya, paths=run.paths, version=version)
    try:
        label(run, ctx, laya, query(wh, _RECORDS_SQL, hashes), samples=None,
              stage="candidate_inference")  # fmt: skip
    finally:
        laya.unload()


def gold_records(run: Run, wh: duckdb.DuckDBPyConnection) -> pa.Table:
    """Records of every gold hash (labeled by the teacher, never appended to ``teacher/``)."""
    return query(wh, _RECORDS_SQL, sorted(run.store.gold_hashes()))
