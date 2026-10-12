"""ET03-01 Phase 4 acceptance: the Laya student against the teacher and the human gold (T03-36).

ET03-01 runs spec 11's public `run_classifier` (T11-35) on the target PC against the phase 4
Laya data root and the frozen 1-2k gold set. Per question the accepted student must meet its
design 03 §5.5 thresholds (the `accepted_criteria` gate check, which also recomputes every
stored gate metric from the gold and cache rows), ECE <= 0.05 (score questions <= 0.06) and a
gap to the teacher of at most 2 percentage points; the comparison against the human labels is
published in the report. Nothing is re-implemented here: the cross-check, the calibration
metrics and the decider comparison all come from `run_classifier`.

The gold set needs the gold labelers of D17 and the Laya model needs V-11/V-09, so the row
skips at runtime with a reason naming D17 until `HERNESS_EVAL_LAYA_MODEL_DIR` (a data root
whose `models/laya/CURRENT` names the phase 4 version) and `HERNESS_EVAL_GOLD_SET` (the
frozen gold directory, `<data root>/labels/<question set version>/gold`) are set, the same
switches as ET11-03 (`HERNESS_EVAL_CONFIG_DIR` optionally names another config tree than the
repo's, whose question set must be the model's). With both set the row really executes and
fails loudly on any misconfiguration (an unloadable config, a gold path that is not the data
root's gold directory, a question set that differs from the model's).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Final

import pytest

from herness.core import config as herness_config
from herness.core.config import HernessConfig
from herness.enrich.laya_models import LayaManifest, read_current, verify_model_dir
from herness.enrich.questions import acceptance_for
from herness.eval.classifier import (
    ClassifierInputs,
    ClassifierReport,
    classifier_inputs,
    run_classifier,
)

pytestmark = [pytest.mark.eval, pytest.mark.gpu, pytest.mark.slow]

_MODEL_DIR: Final = "HERNESS_EVAL_LAYA_MODEL_DIR"
_GOLD: Final = "HERNESS_EVAL_GOLD_SET"
_BLOCKED: Final = (
    "blocked by D17 (gold labelers): needs the phase 4 Laya data root and the frozen gold set "
    "on the target PC ({missing} not set)"
)
_ECE_MAX: Final = 0.05
_ECE_MAX_SCORE: Final = 0.06
_GAP_MAX: Final = 0.02
_GOLD_MIN: Final = 1_000  # the row's 1-2k gold range
_GOLD_MAX: Final = 2_000
_CONFIG_DIR: Final = "HERNESS_EVAL_CONFIG_DIR"  # optional: the config tree of the phase 4 model
_SHIPPED: Final = Path(__file__).resolve().parents[3] / "config"


def _require(*names: str) -> None:
    missing = [name for name in names if not os.environ.get(name)]
    if missing:
        pytest.skip(_BLOCKED.format(missing=", ".join(missing)))


def test_et03_01_phase4_student_vs_teacher_vs_human_on_gold() -> None:
    """ET03-01 per accepted question: §5.5 acceptance criteria, ECE <= 0.05 (score <= 0.06)
    and gap to the teacher <= 2 pp on the frozen gold; gate cross-check passes."""
    _require(_MODEL_DIR, _GOLD)
    data_root = Path(os.environ[_MODEL_DIR]).resolve()
    config_dir = Path(os.environ.get(_CONFIG_DIR) or _SHIPPED)
    herness_config.reset_config()
    cfg = herness_config.init_config(
        "local", config_dir=config_dir, env={"HERNESS_PATHS__DATA": str(data_root)}
    )
    assert cfg.eval is not None, "config/eval.yaml is not loaded"
    inputs = classifier_inputs(cfg, data_root)
    version = read_current(inputs.paths)
    manifest = verify_model_dir(inputs.paths, version)
    qsv = manifest.question_set_version
    assert qsv == inputs.questions.version, "the configured question set is not the model's"
    gold_dir = inputs.paths.labels_dir(qsv, "gold")
    assert Path(os.environ[_GOLD]).resolve() == gold_dir, f"{_GOLD} is not {gold_dir}"
    assert manifest.accepted_questions, "the phase 4 model accepts no question"

    try:
        report = run_classifier(
            qsv, inputs.deciders, data_root=data_root, warehouse=None, truth_dir=None,
            settings=cfg.eval.classifier, inputs=inputs,
        )  # fmt: skip
        _check(cfg, inputs, manifest, report)
    finally:
        herness_config.reset_config()


def _check(cfg: HernessConfig, inputs: ClassifierInputs, manifest: LayaManifest,
           report: ClassifierReport) -> None:  # fmt: skip
    failed = [(c.check, c.question, c.metric, c.detail) for c in report.gate_check if not c.passed]
    assert not failed, f"gate cross-check failed: {failed}"
    laya, teacher = report.deciders["laya"], report.deciders[manifest.teacher]
    for qid in manifest.accepted_questions:
        student, reference = laya[qid], teacher[qid]
        assert student.status == "ok", f"{qid}: no student metrics on the gold"
        assert _GOLD_MIN <= student.n <= _GOLD_MAX, f"{qid}: gold size {student.n} not 1-2k"
        assert reference.status == "ok", f"{qid}: no teacher metrics on the gold"
        # the RECOMPUTED metrics against the configured design 03 section 5.5 thresholds
        crit = acceptance_for(cfg.decisions, qid)
        for name, value in (
            ("min_accuracy", student.accuracy), ("min_macro_f1", student.macro_f1),
            ("min_coverage", student.coverage_at_threshold),
            ("min_within_one", student.within_one),
        ):  # fmt: skip
            floor = getattr(crit, name)
            if floor is not None:
                assert value is not None, f"{qid}: {name} has no recomputed metric"
                assert value >= floor, f"{qid}: {name} {value:.4f} < {floor}"
        if crit.max_mae is not None:
            assert student.mae is not None
            assert student.mae <= crit.max_mae, f"{qid}: MAE {student.mae:.4f} > {crit.max_mae}"
        assert student.ece is not None
        score = inputs.questions.get(qid).type == "score"
        limit = min(_ECE_MAX_SCORE if score else _ECE_MAX, crit.max_ece or 1.0)
        assert student.ece <= limit, f"{qid}: ECE {student.ece:.4f} > {limit}"
        assert student.accuracy is not None
        assert reference.accuracy is not None
        gap_limit = min(_GAP_MAX, crit.max_gap_to_teacher or _GAP_MAX)
        gap = reference.accuracy - student.accuracy
        assert gap <= gap_limit, f"{qid}: gap to the teacher {gap:.4f} > {gap_limit}"
        assert report.deciders["human"][qid].status in {"ok", "insufficient", "absent"}
