"""Unit tests of the classifier eval (U11-67, U11-68 step 3, U11-70 section; T11-35).

Gate checks, `ConfigError` paths, the decider comparison and its version choice, the human
comparison, the synthetic check, the metric helpers, the report section's escaping and the
runner branch (exit 14 on a failed check, a failed row when writing fails). The fixture model
directory is `_classifier_env` (impl 03's `evaluate_candidate` output); bootstrap runs with
100 resamples here.
"""

from __future__ import annotations

import json
import types
from pathlib import Path
from typing import Any

import duckdb
import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from sklearn.metrics import f1_score  # type: ignore[import-untyped]
from structlog.testing import capture_logs
from tests.support.ops_store import OpsStoreHandle
from tests.unit.enrich import _evaluate_fixtures as fx
from tests.unit.eval._classifier_env import (
    QSV,
    VERSION,
    ClassifierEnv,
    classifier_env,
    set_eval_metric,
)
from tests.unit.eval._runner_env import FakeCtx, make_deps, runner_store, test_redactor
from tests.unit.eval.test_eval_report import eval_run

from herness.core.errors import ConfigError
from herness.core.logging import bind_ids
from herness.core.types import Question, QuestionSet
from herness.enrich.cache import CACHE_SCHEMA, write_part
from herness.enrich.calibrate import ece
from herness.enrich.labels import HUMAN_SCHEMA
from herness.eval import _classifier_deciders as dec
from herness.eval import _classifier_metrics as cm
from herness.eval import _runner_classifier, classifier, runner, truth
from herness.eval.classifier import (
    ClassifierReport,
    DeciderMetrics,
    GateMetricCheck,
    default_deciders,
    run_classifier,
)
from herness.eval.metrics import EvalMetrics
from herness.eval.report import write_report
from herness.eval.runner import exit_code
from herness.eval.settings import ClassifierSettings
from herness.store.ops import get_run
from herness.store.ops.core import read_all

pytestmark = pytest.mark.unit
__all__ = ["classifier_env", "runner_store", "test_redactor"]  # fixtures

FAST = ClassifierSettings(gate_recompute_tolerance=0.005, synthetic_sample=100, bootstrap=100)
ATTACK = "<script>alert(1)</script>|`x`"


def _run(env: ClassifierEnv, **kwargs: Any) -> ClassifierReport:
    return env.run(**({"settings": FAST} | kwargs))


def _failed(report: ClassifierReport) -> list[tuple[str, str | None, str | None]]:
    return [(c.check, c.question, c.metric) for c in report.gate_check if not c.passed]


# --- gate checks (F11-09 steps 1-4) -------------------------------------------------------------


def test_cv_t11_35_fixture_passes_every_gate_check(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 the evaluate_candidate fixture passes: model and question set version, gold
    digest, cache rows, every stored metric, calibration and the accepted question."""
    report = _run(classifier_env)
    assert report.passed is True
    assert _failed(report) == []
    names = [c.check for c in report.gate_check]
    assert names[:3] == ["model_version", "question_set_version", "gold_sha256"]
    assert names.count("metric") == 16  # 8 stored fields x 2 questions
    assert report.check("accepted_criteria") is not None
    assert report.check("question") is None


def test_cv_t11_35_tolerance_comes_from_settings(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 a 0.004 change passes at the configured 0.005 and fails at 0.003."""
    set_eval_metric(classifier_env, "change_caused", "ece", 0.004)
    assert _run(classifier_env).passed is True
    tight = FAST.model_copy(update={"gate_recompute_tolerance": 0.003})
    assert _failed(_run(classifier_env, settings=tight)) == [("metric", "change_caused", "ece")]


def test_cv_t11_35_none_versus_value_is_a_mismatch(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 a stored null where a metric was recomputed (and back) fails the check."""
    doc = classifier_env.eval_doc()
    doc["questions"]["root_cause"]["laya"]["macro_f1"] = None
    doc["questions"]["change_caused"]["laya"]["mae"] = 0.5
    classifier_env.write_eval(doc)
    assert _failed(_run(classifier_env)) == [
        ("metric", "change_caused", "mae"),
        ("metric", "root_cause", "macro_f1"),
    ]


def test_cv_t11_35_missing_cache_rows_fail_naming_the_question(
    classifier_env: ClassifierEnv,
) -> None:
    """CV-T11-35 Laya cache rows missing for gold hashes: `cache_rows` fails for the question."""
    part = next(classifier_env.paths.cache_partition(QSV, "laya", VERSION).glob("part-*"))
    table = pq.read_table(part)
    keep = [not (q == "change_caused" and i % 7 == 0)
            for i, q in enumerate(table.column("question").to_pylist())]  # fmt: skip
    pq.write_table(table.filter(pa.array(keep)), part)
    report = _run(classifier_env)
    rows = report.check("cache_rows")
    assert rows is not None
    failed = [c for c in report.gate_check if c.check == "cache_rows" and not c.passed]
    assert [(c.question, c.detail) for c in failed] == [
        ("change_caused", "missing Laya cache rows")
    ]
    assert failed[0].stored == 60.0
    assert failed[0].recomputed is not None
    assert failed[0].recomputed < 60.0


def test_cv_t11_35_calibration_file_temperature_checked(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 `calibration.json`'s temperature must match `eval.json`; a missing entry fails."""
    path = classifier_env.laya_dir / "calibration.json"
    doc = json.loads(path.read_text(encoding="utf-8"))
    doc["questions"]["root_cause"]["temperature"] += 0.05
    del doc["questions"]["change_caused"]
    path.write_text(json.dumps(doc), encoding="utf-8")
    report = _run(classifier_env)
    failed = [(c.question, c.detail) for c in report.gate_check if not c.passed]
    assert failed == [("change_caused", "no calibration.json entry"), ("root_cause", "")]


def test_cv_t11_35_accepted_question_criteria(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 step 4: an accepted question with a false criterion, without criteria or
    missing from `eval.json` fails."""
    doc = classifier_env.eval_doc()
    doc["questions"]["root_cause"]["passed"]["min_accuracy"] = False
    doc["questions"]["change_caused"]["passed"] = {}
    classifier_env.write_eval(doc)
    manifest_path = classifier_env.laya_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["accepted_questions"] = ["root_cause", "change_caused", "business_impact"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = _run(classifier_env)
    failed = [(c.question, c.detail) for c in report.gate_check if c.check == "accepted_criteria"]
    assert failed == [("root_cause", "min_accuracy"), ("change_caused", "no criteria"),
                      ("business_impact", "not in eval.json")]  # fmt: skip
    assert not any(c.passed for c in report.gate_check if c.check == "accepted_criteria")


def test_cv_t11_35_question_rows_and_versions(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 an eval.json question not in the set, a changed fingerprint, a question
    without frozen gold, another model or question set version: each a failed check."""
    doc = classifier_env.eval_doc()
    doc["version"] = "laya-20261004-9"
    doc["question_set_version"] = "qs-2026-09-01.1"
    doc["questions"]["ghost"] = doc["questions"]["root_cause"]
    doc["questions"]["change_caused"]["question_fingerprint"] = "0" * 16
    doc["questions"]["business_impact"] = dict(
        doc["questions"]["root_cause"],
        question_fingerprint=classifier_env.fixture.qs.get("business_impact").fingerprint,
    )
    classifier_env.write_eval(doc)
    report = _run(classifier_env)
    rows = {(c.check, c.question): c.detail for c in report.gate_check if not c.passed}
    assert rows == {
        ("model_version", None): "", ("question_set_version", None): "",
        ("question", "business_impact"): "no frozen gold",
        ("question", "change_caused"): "fingerprint changed",
        ("question", "ghost"): "not in the question set",
    }  # fmt: skip


def test_cv_t11_35_every_failed_check_is_logged(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 each failed check logs `eval.classifier.mismatch` (ERROR) with the bound
    run id, question, metric, stored and recomputed; passing checks log nothing."""
    set_eval_metric(classifier_env, "root_cause", "coverage_at_threshold", -0.02)
    doc = classifier_env.eval_doc()
    doc["gold_sha256"] = "f" * 64
    classifier_env.write_eval(doc)
    run_id = "run_01M4M002341HCJCEY2D9DSJFH7"
    with capture_logs() as logs, bind_ids(run_id=run_id):
        _run(classifier_env)
    events = [e for e in logs if e["event"] == "eval.classifier.mismatch"]
    assert [(e["question"], e["metric"]) for e in events] == [
        (None, "gold_sha256"),
        ("root_cause", "coverage_at_threshold"),
    ]
    assert {e["run_id"] for e in events} == {run_id}
    assert {e["log_level"] for e in events} == {"error"}
    assert set(events[1]) >= {"stored", "recomputed"}


# --- ConfigError paths --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("damage", "message"),
    [
        (lambda env: (env.paths.laya_root() / "CURRENT").unlink(), "laya CURRENT"),
        (lambda env: (env.laya_dir / "eval.json").unlink(), "eval.json missing"),
        (lambda env: (env.laya_dir / "eval.json").write_text("[1]", "utf-8"), "malformed"),
        (lambda env: (env.laya_dir / "eval.json").write_bytes(b" " * (4 * 1024 * 1024 + 1)),
         "larger than 4 MB"),
        (lambda env: (env.laya_dir / "manifest.json").unlink(), "manifest missing"),
        (lambda env: [p.unlink() for p in env.paths.labels_dir(QSV, "gold").glob("part-*")],
         "gold set missing"),
    ],
)  # fmt: skip
def test_cv_t11_35_missing_inputs_are_config_errors(
    classifier_env: ClassifierEnv, damage: Any, message: str
) -> None:
    """CV-T11-35 missing `CURRENT`, `eval.json` (or malformed or oversized), manifest or
    gold → `ConfigError`; through the handler no run row is written and the exit code is 3."""
    damage(classifier_env)
    with pytest.raises(ConfigError, match=message):
        _run(classifier_env)
    with pytest.raises(ConfigError, match=message):
        classifier_env.handle()
    assert read_all("SELECT run_id FROM run WHERE kind = 'eval'", ()) == []
    assert exit_code({"status": "failed", "last_error": {"class": "ConfigError"}}) == 3


def test_cv_t11_35_question_set_must_be_the_configured_one(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 a question set version other than the configured one is `ConfigError`."""
    with pytest.raises(ConfigError, match="is not the configured one"):
        run_classifier("qs-2026-09-01.1", ["laya"], data_root=classifier_env.data_root,
                       warehouse=None, truth_dir=None, settings=FAST)  # fmt: skip


# --- decider comparison (step 5) ----------------------------------------------------------------


def _cache_part(env: ClassifierEnv, decider: str, version: str, n: int, *, start: int = 0) -> None:
    fp = env.fixture.qs.get("root_cause").fingerprint
    rows = [
        {"content_hash": fx.chash(i), "question": "root_cause", "question_fingerprint": fp,
         "answer": fx.gold_answer("root_cause", i), "probability": 0.9,
         "distribution": [(fx.gold_answer("root_cause", i), 0.9), ("unknown", 0.1)],
         "backend_confidence": None, "samples": None, "decided_at": fx.T0}
        for i in range(start, start + n)
    ]  # fmt: skip
    write_part(env.paths.cache_partition(QSV, decider, version),  # type: ignore[arg-type]
               pa.Table.from_pylist(rows, CACHE_SCHEMA))  # fmt: skip


def test_cv_t11_35_decider_versions_and_absent(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 laya uses `CURRENT`, openjev the manifest teacher's version; other deciders
    the version with the most gold rows (ties: lexicographically greatest); a decider without
    cache rows is `absent` for every question."""
    _cache_part(classifier_env, "llm", "llm-a", 40)
    _cache_part(classifier_env, "llm", "llm-b", 40, start=100)
    _cache_part(classifier_env, "ensemble", "ens-a", 50)
    _cache_part(classifier_env, "ensemble", "ens-b", 10)
    report = _run(classifier_env, deciders=["laya", "openjev", "llm", "ensemble", "jev"])
    d = report.deciders
    assert d["laya"]["root_cause"].decider_version == VERSION
    assert d["openjev"]["root_cause"].decider_version == fx.TEACHER[1]
    assert d["llm"]["root_cause"].decider_version == "llm-b"
    assert d["llm"]["root_cause"].n == 40
    assert d["ensemble"]["root_cause"].decider_version == "ens-a"
    assert d["llm"]["change_caused"].status == "absent"
    assert {m.status for m in d["jev"].values()} == {"absent"}
    assert set(d["laya"]) == {"root_cause", "change_caused"}  # frozen gold questions only


def test_cv_t11_35_openjev_without_teacher_manifest(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 with an `llm` teacher in the manifest, openjev's version is the one with the
    most gold rows."""
    manifest_path = classifier_env.laya_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["teacher"] = "llm"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = _run(classifier_env, deciders=["openjev"])
    assert report.deciders["openjev"]["root_cause"].decider_version == fx.TEACHER[1]


def _human_rows(env: ClassifierEnv, qid: str, n: int, *, wrong_every: int = 0) -> None:
    fp = env.fixture.qs.get(qid).fingerprint
    rows = []
    for i in range(n):
        answer = fx.gold_answer(qid, i)
        if wrong_every and i % wrong_every == 0:
            answer = "false" if answer == "true" else "true"
        rows.append({"content_hash": fx.chash(i), "record_id": f"INC-{i}", "question": qid,
                     "question_fingerprint": fp, "answer": answer, "labeled_by": fx.USER,
                     "labeled_at": fx.T0, "item_id": f"h_{qid}_{i}"})  # fmt: skip
    env.fixture.store.append("human", pa.Table.from_pylist(rows, schema=HUMAN_SCHEMA))


def test_cv_t11_35_human_insufficient_and_ok(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 human labels: fewer than 30 gold overlaps `insufficient` (n kept), 30 or more
    scored as hard answers (no ECE, no temperature)."""
    _human_rows(classifier_env, "root_cause", 29)
    _human_rows(classifier_env, "change_caused", 40, wrong_every=4)
    human = _run(classifier_env, deciders=["human"]).deciders["human"]
    assert (human["root_cause"].status, human["root_cause"].n) == ("insufficient", 29)
    cc = human["change_caused"]
    assert (cc.status, cc.n, cc.accuracy) == ("ok", 40, 0.75)
    assert cc.ece is None
    assert cc.temperature is None
    assert cc.macro_f1 is not None
    assert sum(map(sum, cc.confusion)) == 40


def test_cv_t11_35_default_deciders(monkeypatch: pytest.MonkeyPatch) -> None:
    """CV-T11-35 step 5 deciders: jev only when enabled."""
    base = ["laya", "openjev", "llm", "ensemble", "human"]
    for enabled, expected in ((False, base), (True, [*base[:2], "jev", *base[2:]])):
        jev = types.SimpleNamespace(enabled=enabled)
        cfg = types.SimpleNamespace(models=types.SimpleNamespace(
            deciders=types.SimpleNamespace(jev=jev)))  # fmt: skip
        monkeypatch.setattr(classifier, "get_config", lambda cfg=cfg: cfg)
        assert default_deciders() == expected


# --- metric helpers -----------------------------------------------------------------------------


def _q(qtype: str, **extra: Any) -> Question:
    base: dict[str, Any] = {"id": f"q_{qtype}", "type": qtype, "applies_to": ("incident",),
                            "instructions": "A test question.", "threshold": 0.7}  # fmt: skip
    if qtype == "choice":
        base["options"] = {"a": "a", "b": "b", "c": "c"}
    if qtype == "score":
        base["levels"] = ("none", "team", "department", "outage")
    return Question.model_validate(base | extra)


def test_cv_t11_35_hard_metrics_match_sklearn_and_ece() -> None:
    """CV-T11-35 macro-F1 is sklearn's for bool and choice; the top-label ECE of hard answers
    equals impl 03's `ece`; 15 reliability bins; confusion counts; CIs bracket the value."""
    rng = np.random.default_rng(1)
    labels = rng.integers(0, 2, 300)
    pred = np.where(rng.random(300) < 0.8, labels, 1 - labels)
    conf = rng.uniform(0.5, 1.0, 300)
    q = _q("bool")
    m = cm.hard_metrics(q, ("true", "false"), pred, labels, conf, version=None,
                        rng=np.random.default_rng(7), resamples=200)  # fmt: skip
    assert m.macro_f1 == pytest.approx(f1_score(labels, pred, average="macro"))
    top = np.where(pred == labels, 0, 1)
    assert m.ece == pytest.approx(ece(np.column_stack([conf, np.zeros(300)]), top))
    assert len(m.reliability) == 15
    assert sum(b.n for b in m.reliability) == 300
    assert sum(map(sum, m.confusion)) == 300
    assert m.confusion[0][0] == int(np.sum((labels == 0) & (pred == 0)))
    for name in ("accuracy", "macro_f1", "ece"):
        low, high = m.ci[name]
        assert low <= getattr(m, name) <= high
    again = cm.hard_metrics(q, ("true", "false"), pred, labels, conf, version=None,
                            rng=np.random.default_rng(7), resamples=200)  # fmt: skip
    assert again == m  # same seed, same intervals


def test_cv_t11_35_score_metrics_and_empty() -> None:
    """CV-T11-35 score: MAE and within-one (no macro-F1); no rows → `absent`."""
    q = _q("score")
    labels = np.array([0, 1, 2, 3, 3])
    pred = np.array([0, 2, 0, 3, 2])
    m = cm.hard_metrics(q, ("0", "1", "2", "3"), pred, labels, None, version="v",
                        rng=np.random.default_rng(0), resamples=100)  # fmt: skip
    assert (m.mae, m.within_one, m.macro_f1, m.ece) == (pytest.approx(0.8), 0.8, None, None)
    assert m.coverage_at_threshold is None
    assert set(m.ci) == {"accuracy", "mae", "within_one"}
    empty = np.array([], dtype=np.int64)
    gone = cm.hard_metrics(q, ("0",), empty, empty, None, version="v",
                           rng=np.random.default_rng(0), resamples=100)  # fmt: skip
    assert (gone.status, gone.decider_version) == ("absent", "v")
    none = cm.scored_metrics(q, ("0",), np.zeros((0, 4)), empty, empty, version=None,
                             rng=np.random.default_rng(0), resamples=100)  # fmt: skip
    assert none.status == "absent"


def test_cv_t11_35_label_space_and_matrix() -> None:
    """CV-T11-35 label space as impl 03 (options, then other answers sorted; fixed for bool and
    score); an all-zero distribution becomes uniform."""
    assert cm.label_space(_q("choice"), ["z", "a", "y"]) == ("a", "b", "c", "y", "z")
    assert cm.label_space(_q("bool"), ["maybe"]) == ("true", "false")
    probs = cm.matrix([{"a": 2.0, "b": 2.0}, {}], ("a", "b"))
    assert probs.tolist() == [[0.5, 0.5], [0.5, 0.5]]


def test_cv_t11_35_stratified_sample() -> None:
    """CV-T11-35 step 6 sample: all rows when few; else proportional per stratum, at least
    20 (or the whole stratum)."""
    rng = np.random.default_rng(3)
    assert dec._stratified(["a"] * 5, 100, rng) == [0, 1, 2, 3, 4]
    strata = ["a"] * 900 + ["b"] * 90 + ["c"] * 10
    picked = dec._stratified(strata, 100, rng)
    counts = {s: sum(1 for i in picked if strata[i] == s) for s in "abc"}
    assert counts == {"a": 90, "b": 20, "c": 10}
    assert len(set(picked)) == len(picked)


def test_cv_t11_35_synthetic_metrics_join_and_missing_table() -> None:
    """CV-T11-35 step 6: decisions of the question set joined to the planted answers by
    record and question; a warehouse without `enrich.decision` gives no section rows."""
    con = duckdb.connect()
    con.execute("CREATE SCHEMA enrich")
    con.execute("CREATE TABLE enrich.decision (record_id VARCHAR, question VARCHAR, answer "
                "VARCHAR, probability DOUBLE, question_set_version VARCHAR)")  # fmt: skip
    rows = [(f"r{i}", "q_bool", "true" if i % 4 else "false", 0.9, "qs-2026-10-01.1")
            for i in range(40)]  # fmt: skip
    rows.append(("r0", "q_bool", "true", 0.9, "qs-2026-09-01.1"))  # another set: ignored
    con.executemany("INSERT INTO enrich.decision VALUES (?, ?, ?, ?, ?)", rows)
    planted = pa.Table.from_pylist([{"record_id": f"r{i}", "content_hash": "0" * 32,
                                     "question": "q_bool", "answer": "true"}
                                    for i in range(50)])  # fmt: skip
    qs = QuestionSet(version="qs-2026-10-01.1", questions=(_q("bool"), _q("score")))
    boot = dec.Bootstrap(np.random.default_rng(0), 100)
    out = dec.synthetic_metrics(qs, qs.version, con, planted, sample=100, boot=boot)
    assert set(out) == {"q_bool"}
    assert (out["q_bool"].n, out["q_bool"].accuracy) == (40, 0.75)
    assert out["q_bool"].coverage_at_threshold == 1.0
    empty = dec.synthetic_metrics(qs, qs.version, duckdb.connect(), planted, sample=100, boot=boot)
    assert empty["q_bool"].status == "absent"


def test_cv_t11_35_truth_labels_reader(tmp_path: Path) -> None:
    """CV-T11-35 `load_truth_labels` reads the four scoring columns (no `pii_spans`); an
    unreadable file is `ConfigError`."""
    table = pa.Table.from_pylist([{"record_id": "r", "content_hash": "h", "question": "q",
                                   "answer": "a", "pii_spans": "[]"}])  # fmt: skip
    pq.write_table(table, tmp_path / "truth_labels.parquet")
    assert truth.load_truth_labels(tmp_path).column_names == list(truth.TRUTH_LABEL_COLUMNS)
    with pytest.raises(ConfigError, match="cannot read truth labels"):
        truth.load_truth_labels(tmp_path / "none")


# --- report section and runner branch -----------------------------------------------------------


def _attack_report() -> ClassifierReport:
    check = GateMetricCheck(check="metric", passed=False, question=ATTACK, metric="accuracy",
                            stored=0.5, recomputed=0.4, detail=ATTACK)  # fmt: skip
    ok = DeciderMetrics(status="ok", decider_version=ATTACK, n=3, accuracy=0.5,
                        ci={"accuracy": (0.25, 0.75)})  # fmt: skip
    return ClassifierReport(version="laya-20261004-1", question_set_version="qs-2026-10-01.1",
                            gate_check=[check], deciders={"llm": {ATTACK: ok}},
                            synthetic={ATTACK: ok}, passed=False)  # fmt: skip


def test_cv_t11_35_report_section_is_escaped(tmp_path: Path) -> None:
    """CV-T11-35 stored identifiers in the classifier section: inline code spans with escaped
    cells in Markdown; autoescaped in HTML (TH11-05)."""
    out = tmp_path / "run"
    write_report(eval_run(out), out, results=[], metrics=EvalMetrics(), thresholds=[],
                 baseline=None, classifier=_attack_report())  # fmt: skip
    md = (out / "report.md").read_text(encoding="utf-8")
    html = (out / "report.html").read_text(encoding="utf-8")
    assert "`` <script>alert(1)</script>\\|`x` ``" in md
    assert "gate cross-check FAILED" in md
    assert "### Synthetic truth check (informational)" in md
    assert "0.5 [0.25, 0.75]" in md
    assert "<script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "Synthetic truth check (informational)" in html


def test_ut11_109_failed_classifier_check_exits_14(classifier_env: ClassifierEnv) -> None:
    """UT11-109 a failed classifier check maps to 14 (and a passing run to 0)."""
    _, ok = classifier_env.handle()
    assert exit_code(ok.model_dump()) == 0
    set_eval_metric(classifier_env, "change_caused", "accuracy", -0.01)
    _, failed = classifier_env.handle()
    assert failed.result == {"run_id": failed.result["run_id"], "passed": False, "exit_code": 14}
    assert exit_code(failed.model_dump()) == 14
    assert exit_code(failed.result) == 14


def test_cv_t11_35_row_failed_when_the_report_cannot_be_written(
    classifier_env: ClassifierEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-35 an error after the row is inserted closes it `failed` and propagates."""

    def boom(*_args: object, **_kwargs: object) -> None:
        msg = "disk full"
        raise OSError(msg)

    monkeypatch.setattr(_runner_classifier, "write_report", boom)
    with pytest.raises(OSError, match="disk full"):
        classifier_env.handle()
    rows = read_all("SELECT status, meta FROM run WHERE kind = 'eval'", ())
    assert [r[0] for r in rows] == ["failed"]
    assert json.loads(rows[0][1])["suite"] == "classifier"


def test_cv_t11_35_run_row_meta_and_summary(classifier_env: ClassifierEnv) -> None:
    """CV-T11-35 the run row (`suite = classifier`, question set and model version, gold
    digest, deciders, passed) and the summary's provenance keys (no judge, no golden suite)."""
    _, outcome = classifier_env.handle(profile="hybrid", depth="deep")
    run_id = outcome.result["run_id"]
    row = get_run(run_id)
    assert row is not None
    assert (row.kind, row.profile, row.depth, row.status) == ("eval", "hybrid", "deep", "done")
    assert row.meta["suite"] == "classifier"
    assert row.meta["question_set_version"] == QSV
    assert row.meta["deciders"] == ["laya", "openjev", "llm", "ensemble", "human"]
    assert row.meta["suite_sha256"] == row.meta["gold_sha256"]  # the gold digest
    summary = json.loads((classifier_env.out_dir(run_id) / "summary.json").read_text("utf-8"))
    assert summary["config_hash"] == row.config_hash
    assert (summary["judge_model"], summary["judge_prompt_sha256"]) == (None, None)
    assert summary["suite_version"] is None
    assert summary["suite_sha256"] == row.meta["gold_sha256"]
    assert summary["metrics"] == EvalMetrics().model_dump(mode="json")
    assert summary["thresholds"] == []


def test_cv_t11_35_handler_needs_a_build(
    runner_store: OpsStoreHandle, monkeypatch: pytest.MonkeyPatch
) -> None:
    """CV-T11-35 `--suite classifier` without a current warehouse build is `ConfigError`."""
    monkeypatch.setattr(runner._Binding, "factory", None)
    con = duckdb.connect()
    runner.set_deps_factory(lambda c: make_deps(runner_store.data_root, con, job=c))
    with pytest.raises(ConfigError, match="current build"):
        runner.handle_eval(FakeCtx({"suite": "classifier", "profile": "local", "repeat": 1}))  # type: ignore[arg-type]
