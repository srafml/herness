"""Distillation round flows F03-13, F03-14 and F03-18 (IT03-15; T03-32).

Stub teacher, fake Laya trainer and seeded gold reviews on the real migrated ops store
(`tests.unit.enrich._distill_support`). Every function carries IT03-15: they check the
candidate directory, gold exclusion (TH03-04), resume, GPU scopes, the stop rule, the active
round and spot-check blocking of the same flow.
"""

from __future__ import annotations

import json
import shutil

import numpy as np
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support.fake_laya import write_laya_version
from tests.support.ops_store import OpsStoreHandle
from tests.unit.enrich._distill_support import (
    QSV,
    DistillEnv,
    FakeCtx,
    build_env,
    decisions,
    read_json,
    stub_answer,
)
from tests.unit.enrich._openjev_support import jev_env

from herness.core.errors import ConfigError, ModelUnavailable, SchemaViolation, StoreBusy
from herness.core.resilience import ProcessState
from herness.enrich import _distill_steps as steps
from herness.enrich.distill import YieldRequested, make_distill_handler, run_distill
from herness.enrich.labels import HUMAN_SCHEMA
from herness.enrich.laya_models import verify_model_dir
from herness.enrich.review_items import iter_review_items

pytestmark = pytest.mark.integration

__all__ = ["jev_env"]  # the fixture is used by name


@pytest.fixture
def env(
    jev_env: ProcessState, ops_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> DistillEnv:
    del jev_env
    return build_env(ops_store.data_root, monkeypatch)


def _spot_items() -> list[dict[str, object]]:
    match = {"question_set_version": QSV, "purpose": "spot_check"}
    return [
        dict(i.payload) for i in iter_review_items("label_check", "pending", payload_match=match)
    ]


def test_it03_15_initial_round_writes_a_candidate(env: DistillEnv) -> None:
    """IT03-15 run_distill("initial"): candidate with manifest, calibration, eval.json; gold
    never in the training set; CURRENT untouched; spot-checks created."""
    ctx = FakeCtx()
    with capture_logs() as logs:
        report = run_distill(round_kind="initial", ctx=ctx.as_ctx())

    assert report.version is not None
    assert not report.stopped
    assert report.stop_reason == "none"
    assert (report.round_kind, report.round, report.teacher) == ("initial", 0, "openjev")
    assert report.teacher_version == "openjev-test"
    assert report.gold_frozen_questions == ["q_b", "q_c"]
    assert report.n_sample == 160
    assert report.n_train + report.n_val > 0
    assert set(report.durations_s) == {"prepared", "teacher_done", "trained", "evaluated"}
    directory = env.paths.laya_dir(report.version)
    for name in ("manifest.json", "calibration.json", "eval.json", "model.safetensors"):
        assert (directory / name).is_file(), name
    manifest = verify_model_dir(env.paths, report.version, require_status=frozenset({"candidate"}))
    assert manifest.teacher == "openjev"
    assert manifest.parent_version is None
    assert manifest.base_checkpoint == steps.BASE_CHECKPOINT
    assert manifest.hyperparams["trainer"] == "sft"
    assert manifest.hyperparams["seed"] == 0
    assert (manifest.hyperparams["round"], manifest.hyperparams["round_kind"]) == (0, "initial")
    assert manifest.n_train == report.n_train
    assert not env.paths.laya_current().exists()
    # TH03-04: gold hashes are labeled by the teacher (cache) but never train
    gold = set(env.gold)
    data = env.trainer.seen[-1]
    trained = set(data.train.column("content_hash").to_pylist())
    trained |= set(data.val.column("content_hash").to_pylist())
    assert trained
    assert not trained & gold
    assert not set(env.teacher_rows().column("content_hash").to_pylist()) & gold
    assert gold <= set(env.teacher.decided)
    assert env.trainer.init_dirs == [env.paths.laya_root() / steps.BASE_DIR]
    evaluated = read_json(directory / "eval.json")
    assert set(evaluated["questions"]) == {"q_b", "q_c"}
    assert evaluated["questions"]["q_c"]["teacher"]["decider"] == "openjev"
    assert report.macro_metric == evaluated["macro_metric"]
    assert env.agent.batch_calls  # candidate inference on the gold hashes
    assert ctx.events == ["enter:decider", "start:openjev", "stop:openjev", "exit:decider"]
    assert ctx.steps() == ["prepared", "teacher_done", "trained", "evaluated"]
    spots = _spot_items()
    assert {p["question"] for p in spots} == {"q_b", "q_c"}
    assert all(p["content_hash"] not in gold for p in spots)
    events = [e["event"] for e in logs]
    assert "enrich.distill.teacher_selected" in events
    assert events.count("enrich.distill.step_completed") == 4


def test_it03_15_handler_runs_the_round(env: DistillEnv) -> None:
    """IT03-15 the handler with an empty payload runs an initial round to ``done``."""
    del env
    outcome = make_distill_handler(None)(FakeCtx().as_ctx())
    assert outcome.status == "done"
    assert outcome.result["round_kind"] == "initial"
    assert outcome.result["stopped"] is False


def test_it03_15_resume_continues_at_the_saved_step(env: DistillEnv) -> None:
    """IT03-15 a yield in training is saved; the rerun skips the teacher and finishes."""
    env.trainer.yield_first = True
    ctx = FakeCtx()
    with pytest.raises(YieldRequested):
        run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert ctx.steps() == ["prepared", "teacher_done"]
    assert ctx.events[-2:] == ["stop:openjev", "exit:decider"]
    decided = len(env.teacher.decided)
    rows = env.teacher_rows().num_rows

    report = run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert ctx.steps() == ["prepared", "teacher_done", "trained", "evaluated"]
    assert len(env.teacher.decided) == decided
    assert env.teacher_rows().num_rows == rows
    assert report.version == ctx.saved[0]["distill"]["version"]
    assert report.n_sample == 160
    assert len(list(env.paths.laya_root().glob("laya-*"))) == 1
    again = run_distill(round_kind="initial", ctx=ctx.as_ctx())  # done: report only
    assert again == report.model_copy(update={"gold_frozen_questions": again.gold_frozen_questions})


def test_it03_15_yield_during_teacher_labels_flushes_and_resumes(env: DistillEnv) -> None:
    """IT03-15 a yield between teacher chunks keeps cached answers; the rerun labels the rest."""
    ctx = FakeCtx(yield_at=1)
    with pytest.raises(YieldRequested, match="teacher"):
        run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert ctx.steps() == ["prepared"]
    assert "stop:openjev" in ctx.events
    report = run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert report.version == ctx.saved[0]["distill"]["version"]
    assert ctx.steps()[-1] == "evaluated"


def test_it03_15_config_error_before_gpu_work(env: DistillEnv) -> None:
    """IT03-15 a decider-ref error (disabled jev in the chain) fails before any GPU scope."""
    cfg = decisions()
    env.set_decisions(cfg.model_copy(update={"escalation_chain": ("jev",)}))
    ctx = FakeCtx()
    with pytest.raises(ConfigError, match="disabled decider"):
        run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert ctx.events == []
    assert ctx.saved == []


def test_it03_15_invalid_saved_state_is_a_config_error(env: DistillEnv) -> None:
    """IT03-15 a tampered saved state is refused (ConfigError), never guessed."""
    del env
    ctx = FakeCtx()
    ctx.saved.append({"distill": {"version": "x"}})
    with pytest.raises(ConfigError, match="saved job state"):
        run_distill(round_kind="initial", ctx=ctx.as_ctx())


def test_it03_15_no_teacher_is_model_unavailable(env: DistillEnv) -> None:
    """IT03-15 OpenJev unhealthy and no LLM client: ModelUnavailable, OpenJev stopped."""
    env.teacher.healthy = False
    ctx = FakeCtx()
    with pytest.raises(ModelUnavailable, match="no distillation teacher"):
        run_distill(round_kind="initial", ctx=ctx.as_ctx())
    assert ctx.events == ["enter:decider", "start:openjev", "stop:openjev", "exit:decider"]


def _blocking_reviews(env: DistillEnv) -> None:
    """100 q_b human rows disagreeing with the stub teacher on non-gold records."""
    q_b = env.qs.get("q_b")
    rows = []
    for n, rec in enumerate(env.records[len(env.gold) : len(env.gold) + 100]):
        answer = stub_answer(rec.hash, q_b)[0]
        rows.append({
            "content_hash": rec.hash, "record_id": rec.record_id, "question": "q_b",
            "question_fingerprint": q_b.fingerprint,
            "answer": "false" if answer == "true" else "true", "labeled_by": "ef" * 16,
            "labeled_at": steps.clock.now(), "item_id": f"h-{n}",
        })  # fmt: skip
    env.store.append("human", pa.Table.from_pylist(rows, schema=HUMAN_SCHEMA))


def test_it03_15_spot_check_disagreement_blocks_a_question(env: DistillEnv) -> None:
    """IT03-15 > 15 % reviewed disagreement with >= 100 reviews blocks q_b from training
    and evaluation (TH03-04 spot-check blocking)."""
    _blocking_reviews(env)
    with capture_logs() as logs:
        report = run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())
    assert report.blocked_questions == ["q_b"]
    data = env.trainer.seen[-1]
    assert [q.id for q in data.questions.questions] == ["q_c"]
    assert set(data.train.column("question").to_pylist()) == {"q_c"}
    assert report.version is not None
    evaluated = read_json(env.paths.laya_dir(report.version) / "eval.json")
    assert set(evaluated["questions"]) == {"q_c"}
    blocked = [e for e in logs if e["event"] == "enrich.distill.question_blocked"]
    assert [(e["question"], e["disagreement"], e["reviews"]) for e in blocked] == [
        ("q_b", 1.0, 100)
    ]


def test_it03_15_spot_check_sizes_and_low_probability_half(env: DistillEnv) -> None:
    """IT03-15 spot-checks: max(min, min(max, 1 %)) capped by the rows; half uniform (hash
    order), half with teacher probability < 0.7."""
    env.set_decisions(decisions(spot_check_min=20, spot_check_max=40))
    run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())
    spots = [p for p in _spot_items() if p["question"] == "q_c"]
    assert len(spots) == 20
    rows = sorted(
        r["content_hash"] for r in env.teacher_rows().to_pylist() if r["question"] == "q_c"
    )
    first_half = set(rows[:10])
    picked = {str(p["content_hash"]) for p in spots}
    assert first_half <= picked
    rest = [p for p in spots if p["content_hash"] not in first_half]
    assert all(float(str(p["probability"])) < 0.7 for p in rest)


# --- active rounds and the stop rule (F03-14) -----------------------------------------------


def _accepted(env: DistillEnv, version: str, *, round_no: int, kind: str,
              parent: str | None, macro: float | None) -> None:  # fmt: skip
    directory = write_laya_version(env.paths.laya_root(), version)
    (directory / "calibration.json").unlink()
    manifest = json.loads((directory / "manifest.json").read_text("utf-8"))
    manifest.update({"parent_version": parent, "question_set_version": QSV,
                     "hyperparams": {"trainer": "sft", "seed": 0, "round": round_no,
                                     "round_kind": kind}})  # fmt: skip
    (directory / "manifest.json").write_text(json.dumps(manifest), "utf-8")
    if macro is not None:
        (directory / "eval.json").write_text(json.dumps({"macro_metric": macro}), "utf-8")
    env.paths.laya_current().write_text(f"{version}\n", "ascii")


def _active(**active: object) -> dict[str, object]:
    base = {"pool": 1000, "candidates": 50, "per_round": 30, "per_round_llm_teacher": 10,
            "per_prototype": 5}  # fmt: skip
    return {"active": {**base, **active}}


def test_it03_15_active_stop_rule_min_gain(env: DistillEnv) -> None:
    """IT03-15 gains below min_gain_pp for `patience` active rounds: stopped, no version,
    CURRENT unchanged, no GPU-side work."""
    env.set_decisions(decisions(**_active()))
    _accepted(env, "laya-20261001-1", round_no=0, kind="initial", parent=None, macro=0.500)
    _accepted(env, "laya-20261002-1", round_no=1, kind="active", parent="laya-20261001-1",
              macro=0.502)  # fmt: skip
    _accepted(env, "laya-20261003-1", round_no=2, kind="active", parent="laya-20261002-1",
              macro=0.503)  # fmt: skip
    ctx = FakeCtx()
    with capture_logs() as logs:
        report = run_distill(round_kind="active", ctx=ctx.as_ctx())
    assert (report.stopped, report.stop_reason, report.version) == (True, "min_gain", None)
    assert report.round == 3
    assert env.paths.laya_current().read_text("ascii").strip() == "laya-20261003-1"
    assert len(list(env.paths.laya_root().glob("laya-*"))) == 3
    assert ctx.saved == []
    assert ctx.events == ["enter:decider", "exit:decider"]
    assert "enrich.distill.stopped" in [e["event"] for e in logs]


def test_it03_15_active_stop_rule_max_rounds(env: DistillEnv) -> None:
    """IT03-15 round >= max_rounds: stopped with ``max_rounds``."""
    env.set_decisions(decisions(**_active(max_rounds=1)))
    _accepted(env, "laya-20261001-1", round_no=0, kind="initial", parent=None, macro=0.5)
    _accepted(env, "laya-20261002-1", round_no=1, kind="active", parent="laya-20261001-1",
              macro=0.6)  # fmt: skip
    report = run_distill(round_kind="active", ctx=FakeCtx().as_ctx())
    assert (report.stopped, report.stop_reason, report.version, report.round) == (
        True, "max_rounds", None, 2)  # fmt: skip


def test_it03_15_active_round_trains_from_current(env: DistillEnv) -> None:
    """IT03-15 active round: CURRENT scores the pool before the teacher starts, at most
    per_round records are labeled, training starts from CURRENT, CURRENT stays."""
    env.set_decisions(decisions(**_active()))
    current = "laya-20261001-1"
    _accepted(env, current, round_no=0, kind="initial", parent=None, macro=None)
    ctx = FakeCtx()
    report = run_distill(round_kind="active", ctx=ctx.as_ctx())
    assert report.version is not None
    assert (report.round, report.n_sample) == (1, 30)
    assert env.trainer.init_dirs == [env.paths.laya_dir(current)]
    manifest = verify_model_dir(env.paths, report.version)
    assert manifest.parent_version == current
    assert manifest.base_checkpoint == current
    assert manifest.hyperparams["round_kind"] == "active"
    assert env.paths.laya_current().read_text("ascii").strip() == current
    rows = env.teacher_rows().to_pylist()
    assert {r["purpose"] for r in rows} == {"active"}
    assert not {r["content_hash"] for r in rows} & set(env.gold)
    scoring = [i for i, e in enumerate(ctx.heartbeats) if e == "active_scoring"]
    teaching = [i for i, e in enumerate(ctx.heartbeats) if e == "teacher"]
    assert scoring
    assert teaching
    assert max(scoring) < min(teaching)


def test_it03_15_active_round_needs_current(env: DistillEnv) -> None:
    """IT03-15 an active round without an accepted CURRENT is a ConfigError."""
    env.set_decisions(decisions(**_active()))
    with pytest.raises(ConfigError):
        run_distill(round_kind="active", ctx=FakeCtx().as_ctx())


def test_it03_15_initial_from_previous_uses_current(env: DistillEnv) -> None:
    """IT03-15 ``init_from: previous`` on an initial round starts from CURRENT."""
    env.set_decisions(decisions(init_from="previous"))
    _accepted(env, "laya-20261001-1", round_no=0, kind="initial", parent=None, macro=None)
    report = run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())
    assert report.version is not None
    assert env.trainer.init_dirs == [env.paths.laya_dir("laya-20261001-1")]


def test_it03_15_vector_reader_reads_by_content_hash(env: DistillEnv) -> None:
    """IT03-15 the sampling `vector_reader` returns stored vectors in hash order, zeros else."""
    from herness.store.vectors import EMBEDDING_DIM, VectorStore  # noqa: PLC0415

    store = VectorStore(env.paths.vectors_dir())
    store.ensure_tables()
    vector = np.ones(EMBEDDING_DIM, dtype=np.float32)
    table = store.table("ticket_embedding")
    row = dict.fromkeys(table.schema.names)
    row.update(
        {
            "record_id": "INC-1",
            "entity": "incident",
            "content_hash": "a" * 32,
            "model": "bge-m3",
            "vector": vector.tolist(),
        }
    )
    table.add(pa.Table.from_pylist([row], schema=table.schema))
    got = steps.vector_reader(env.paths)(["b" * 32, "a" * 32])
    assert got.shape == (2, EMBEDDING_DIM)
    assert not got[0].any()
    assert np.allclose(got[1], 1.0)
    shutil.rmtree(env.paths.vectors_dir())


def test_it03_15_version_collision_is_store_busy(
    env: DistillEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT03-15 step 2: an existing version directory is never reused (StoreBusy)."""
    env.paths.laya_dir("laya-20261004-1").mkdir(parents=True)
    monkeypatch.setattr(steps, "new_version_id", lambda paths, now: "laya-20261004-1")
    with pytest.raises(StoreBusy):
        run_distill(round_kind="initial", ctx=FakeCtx().as_ctx())


def test_it03_15_warehouse_errors_are_schema_violations(env: DistillEnv) -> None:
    """IT03-15 a failing warehouse query raises SchemaViolation naming the error class only."""
    with pytest.raises(SchemaViolation, match="CatalogException"):
        steps.query(env.wh, "SELECT * FROM no_such_table", ["a" * 32])


def test_it03_15_yield_during_active_scoring(env: DistillEnv) -> None:
    """IT03-15 a yield request while CURRENT scores the pool raises before the teacher starts."""
    env.set_decisions(decisions(**_active()))
    _accepted(env, "laya-20261001-1", round_no=0, kind="initial", parent=None, macro=None)
    ctx = FakeCtx(yield_at=1)
    with pytest.raises(YieldRequested, match="active_scoring"):
        run_distill(round_kind="active", ctx=ctx.as_ctx())
    assert "start:openjev" not in ctx.events
    assert ctx.steps() == ["prepared"]
