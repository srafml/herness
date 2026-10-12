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
from herness.enrich.laya_models import read_current, verify_model_dir
from herness.eval.classifier import classifier_inputs, run_classifier

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

    report = run_classifier(
        qsv, inputs.deciders, data_root=data_root, warehouse=None, truth_dir=None,
        settings=cfg.eval.classifier, inputs=inputs,
    )  # fmt: skip

    failed = [(c.check, c.question, c.metric, c.detail) for c in report.gate_check if not c.passed]
    assert not failed, f"gate cross-check failed: {failed}"
    laya, teacher = report.deciders["laya"], report.deciders[manifest.teacher]
    for qid in manifest.accepted_questions:
        criteria = [c for c in report.gate_check if c.check == "accepted_criteria"
                    and c.question == qid]  # fmt: skip
        assert criteria, f"{qid}: no accepted_criteria check ran"
        student, reference = laya[qid], teacher[qid]
        assert student.status == "ok", f"{qid}: no student metrics on the gold"
        assert student.ece is not None
        limit = _ECE_MAX_SCORE if inputs.questions.get(qid).type == "score" else _ECE_MAX
        assert student.ece <= limit, f"{qid}: ECE {student.ece:.4f} > {limit}"
        assert reference.status == "ok", f"{qid}: no teacher metrics on the gold"
        assert student.accuracy is not None
        assert reference.accuracy is not None
        gap = reference.accuracy - student.accuracy
        assert gap <= _GAP_MAX, f"{qid}: gap to the teacher {gap:.4f} > {_GAP_MAX}"
        assert report.deciders["human"][qid].status in {"ok", "insufficient", "absent"}
