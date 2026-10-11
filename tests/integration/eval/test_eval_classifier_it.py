"""Integration rows of the classifier eval (U11-67, flow F11-09; T11-35): IT11-25..IT11-27.

IT11-25 and IT11-26 run `handle_eval` in-process for `--suite classifier` (the `--inline`
path, R-45; the CLI is T09-24's) on the fixture model directory of `_classifier_env`, whose
`eval.json` comes from impl 03's own `evaluate_candidate`. IT11-27 runs on a copy of
`tiny_build` (seed 7 tiny, enriched through the real pipeline on the stub decider): its Laya
and OpenJev cache, `CURRENT` Laya version and `enrich.decision` rows; gold is taken from the
planted labels of a few hundred records, frozen, and gated by `evaluate_candidate`, so the
synthetic section scores the build's stored decisions against `truth_labels.parquet`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import duckdb
import pyarrow as pa
import pytest
from structlog.testing import capture_logs
from tests.support.builds import BuildHandle
from tests.support.fake_laya import write_laya_version
from tests.support.ops_store import OpsStoreHandle
from tests.support.truth import load_truth_labels
from tests.unit.eval._classifier_env import (
    VERSION,
    ClassifierEnv,
    classifier_env,
    set_eval_metric,
)
from tests.unit.eval._runner_env import FakeCtx, make_deps

from herness.core import time as clock
from herness.core.config import get_config
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.evaluate import evaluate_candidate
from herness.enrich.gold import fold_of
from herness.enrich.labels import GOLD_SCHEMA, LabelStore, gold_digest
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set
from herness.eval import runner
from herness.eval.runner import exit_code
from herness.store.ops import get_run

pytestmark = pytest.mark.integration
__all__ = ["classifier_env"]  # fixture

_GOLD_PER_QUESTION = 240


def test_it11_25_cross_check_passes_and_writes_classifier_json(
    classifier_env: ClassifierEnv,
) -> None:
    """IT11-25 fixture `data/models/laya/<v>/` with `eval.json`, gold parts and cache:
    `--suite classifier` cross-check passes; `classifier.json` written."""
    _, outcome = classifier_env.handle()
    assert outcome.status == "done"
    assert outcome.result["passed"] is True
    assert outcome.result["exit_code"] == 0
    assert exit_code(outcome.model_dump()) == 0
    out = classifier_env.out_dir(outcome.result["run_id"])
    report = json.loads((out / "classifier.json").read_text(encoding="utf-8"))
    assert report["version"] == VERSION
    assert report["passed"] is True
    checks = report["gate_check"]
    assert all(c["passed"] for c in checks)
    metrics = {(c["question"], c["metric"]) for c in checks if c["check"] == "metric"}
    assert ("root_cause", "accuracy") in metrics
    assert ("change_caused", "temperature") in metrics
    assert {c["check"] for c in checks} >= {"gold_sha256", "cache_rows", "accepted_criteria"}
    laya = report["deciders"]["laya"]["root_cause"]
    assert laya["status"] == "ok"
    assert laya["n"] == 200
    assert len(laya["reliability"]) == 15
    assert laya["ci"]["accuracy"][0] <= laya["accuracy"] <= laya["ci"]["accuracy"][1]
    assert report["deciders"]["openjev"]["root_cause"]["decider_version"] == "openjev-0.4.0/qwen:7b"
    assert report["deciders"]["human"]["root_cause"]["status"] == "absent"
    assert report["synthetic"] is None  # a real build
    assert {p.name for p in out.iterdir()} == {"summary.json", "report.md", "report.html",
                                               "classifier.json"}  # fmt: skip
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["suite"] == "classifier"
    assert summary["suite_sha256"] == report["gate_check"][2]["recomputed"]
    assert summary["exit_code"] == 0
    row = get_run(outcome.result["run_id"])
    assert row is not None
    assert row.status == "done"
    assert row.meta["model_version"] == VERSION
    assert row.meta["gold_sha256"] == summary["suite_sha256"]
    assert row.meta["passed"] is True
    assert "Classifier" in (out / "report.md").read_text(encoding="utf-8")


def test_it11_26_changed_metric_fails_with_exit_14(classifier_env: ClassifierEnv) -> None:
    """IT11-26 same fixture with one metric changed by 0.01: mismatch check fails; exit 14;
    `eval.classifier.mismatch` names the run, question, metric and both values."""
    changed = set_eval_metric(classifier_env, "root_cause", "accuracy", 0.01)
    with capture_logs() as logs:
        _, outcome = classifier_env.handle()
    assert outcome.result["passed"] is False
    assert outcome.result["exit_code"] == 14
    assert exit_code(outcome.model_dump()) == 14
    out = classifier_env.out_dir(outcome.result["run_id"])
    report = json.loads((out / "classifier.json").read_text(encoding="utf-8"))
    failed = [c for c in report["gate_check"] if not c["passed"]]
    assert [(c["check"], c["question"], c["metric"]) for c in failed] == [
        ("metric", "root_cause", "accuracy")
    ]
    assert failed[0]["stored"] == pytest.approx(changed)
    mismatches = [e for e in logs if e["event"] == "eval.classifier.mismatch"]
    assert len(mismatches) == 1
    event = mismatches[0]
    assert event["run_id"] == outcome.result["run_id"]
    assert (event["question"], event["metric"]) == ("root_cause", "accuracy")
    assert event["stored"] == pytest.approx(changed)
    assert event["recomputed"] == pytest.approx(changed - 0.01)
    assert event["log_level"] == "error"
    row = get_run(outcome.result["run_id"])
    assert row is not None
    assert row.status == "done"
    assert row.meta["passed"] is False


# --- IT11-27 on tiny_build --------------------------------------------------------------------


def _laya_version(build: BuildHandle) -> str:
    return (build.data_root / "models" / "laya" / "CURRENT").read_text("utf-8").strip()


def _teacher_version(paths: EnrichPaths, qsv: str) -> str:
    dataset = DecisionCache(paths, qsv).dataset()
    assert dataset is not None
    table = dataset.to_table(columns=["decider", "decider_version"])
    versions = {v for d, v in zip(*(table.column(n).to_pylist() for n in table.column_names),
                                  strict=True) if d == "openjev"}  # fmt: skip
    assert len(versions) == 1
    return versions.pop()


def _laya_keys(paths: EnrichPaths, qsv: str) -> set[tuple[str, str]]:
    dataset = DecisionCache(paths, qsv).dataset()
    assert dataset is not None
    table = dataset.to_table(columns=["decider", "question", "content_hash"])
    rows = zip(*(table.column(n).to_pylist() for n in table.column_names), strict=True)
    return {(q, h) for d, q, h in rows if d == "laya"}


def _gold_from_truth(build: BuildHandle, store: LabelStore, qsv: str) -> None:
    """Frozen gold for every planted question from planted answers, preferring records the
    fake Laya decided (it answers `root_cause` only)."""
    qs = load_question_set(get_config().decisions)
    truth = load_truth_labels(build.root / "truth").to_pylist()
    decided = _laya_keys(store.paths, qsv)
    now = clock.now()
    for q in qs.questions:
        planted = [r for r in truth if r["question"] == q.id]
        planted.sort(key=lambda r: (q.id, r["content_hash"]) not in decided)
        rows = planted[:_GOLD_PER_QUESTION]
        if not rows:
            continue
        gold = [{"content_hash": r["content_hash"], "record_id": r["record_id"], "question": q.id,
                 "question_fingerprint": q.fingerprint, "answer": r["answer"],
                 "labeled_by": "ab" * 16, "labeled_at": now, "item_id": f"it_{q.id}_{i}",
                 "fold": fold_of(r["content_hash"]), "adjudicated": False}
                for i, r in enumerate(rows)]  # fmt: skip
        table = pa.Table.from_pylist(gold, schema=GOLD_SCHEMA)
        store.append("gold", table)
        assert q.fingerprint is not None
        store.freeze_gold(q.id, q.fingerprint, gold_digest(table), table.num_rows)
    assert qsv == qs.version


def _tiny_model_dir(data_root: Path, build: BuildHandle) -> None:
    """The tiny build's warehouse, cache, Laya version and truth under `data_root`; frozen
    gold from planted answers and `eval.json` from `evaluate_candidate`."""
    for part in ("models/laya", "cache/decisions"):
        shutil.copytree(build.data_root / part, data_root / part)
    (data_root / "warehouse").mkdir()
    shutil.copyfile(build.warehouse, data_root / "warehouse" / build.warehouse.name)
    (data_root / "warehouse" / "CURRENT").write_text(build.build_id, encoding="ascii")
    shutil.copytree(build.root / "truth", data_root.parent / "truth")
    cfg = get_config()
    qsv = cfg.decisions.question_set_version
    paths = EnrichPaths(data_root=data_root, embedding_path="models/e",
                        laya_current_file="data/models/laya/CURRENT")  # fmt: skip
    version = _laya_version(build)
    shutil.rmtree(data_root / "models" / "laya" / version)  # a fresh, hash-consistent dir
    write_laya_version(paths.laya_root(), version)
    (paths.laya_dir(version) / "calibration.json").unlink()
    store = LabelStore(paths, qsv)
    _gold_from_truth(build, store, qsv)
    teacher = ("openjev", _teacher_version(paths, qsv))
    evaluate_candidate(version=version, qs=load_question_set(cfg.decisions), cfg=cfg.decisions,
                       store=store, cache=DecisionCache(paths, qsv),
                       calibration=CalibrationStore(paths), teacher=teacher, paths=paths,
                       now=clock.now())  # fmt: skip
    manifest_path = paths.laya_dir(version) / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest |= {"teacher_version": teacher[1], "accepted_questions": []}
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")


def test_it11_27_synthetic_section_and_absent_deciders(
    ops_store: OpsStoreHandle, tiny_build: BuildHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT11-27 `tiny_build` with stub-decider decisions: classifier eval on synthetic; the
    synthetic section has metrics per question; deciders `absent` where no cache."""
    data_root = ops_store.data_root
    _tiny_model_dir(data_root, tiny_build)
    con = duckdb.connect(str(data_root / "warehouse" / tiny_build.warehouse.name), read_only=True)
    monkeypatch.setattr(runner._Binding, "factory", None)
    runner.set_deps_factory(lambda c: make_deps(data_root, con, job=c))
    try:
        outcome = runner.handle_eval(  # type: ignore[arg-type]
            FakeCtx({"suite": "classifier", "profile": "synth", "repeat": 1})
        )
    finally:
        con.close()
    assert outcome.status == "done"
    out = data_root / "reports" / "eval" / str(outcome.result["run_id"])
    report = json.loads((out / "classifier.json").read_text(encoding="utf-8"))
    assert all(c["passed"] for c in report["gate_check"] if c["check"] == "metric")
    failed = {(c["check"], c["question"]) for c in report["gate_check"] if not c["passed"]}
    no_laya = {"change_caused", "repeat_issue", "business_impact", "owning_team"}
    assert failed == {("cache_rows", qid) for qid in no_laya}  # Laya answers root_cause only
    assert outcome.result["exit_code"] == 14
    synthetic = report["synthetic"]
    planted = {"root_cause", "change_caused", "repeat_issue", "business_impact", "owning_team"}
    assert set(synthetic) == planted
    for qid, m in synthetic.items():
        assert m["status"] == "ok", qid
        assert m["n"] > 0
        assert 0.0 <= m["accuracy"] <= 1.0
        assert m["ece"] is not None
        assert m["ci"]["accuracy"][0] <= m["accuracy"] <= m["ci"]["accuracy"][1]
    assert synthetic["business_impact"]["within_one"] is not None
    assert synthetic["root_cause"]["macro_f1"] is not None
    assert synthetic["change_caused"]["macro_f1"] is not None  # bool too (U11-67)
    deciders = report["deciders"]
    assert set(deciders) == {"laya", "openjev", "llm", "ensemble", "human"}  # jev disabled
    for name in ("llm", "ensemble", "human"):
        assert {m["status"] for m in deciders[name].values()} == {"absent"}, name
    assert deciders["laya"]["root_cause"]["status"] == "ok"
    assert {deciders["laya"][qid]["status"] for qid in no_laya} == {"absent"}
    assert {m["status"] for m in deciders["openjev"].values()} == {"ok"}
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["dataset_kind"] == "synthetic"
    assert summary["dataset_seed"] == tiny_build.truth.seed
