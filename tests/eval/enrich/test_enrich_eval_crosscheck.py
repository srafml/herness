"""ET03-02 ``eval.json`` cross-check (spec 11 §5.4; T03-30, TH03-17).

Recomputes every gate metric of ``eval.json`` from the raw gold parts and cache parts with the
spec 03 functions spec 11 imports (``cross_fit``, ``question_metrics``, ``gold_digest``), without
going through ``evaluate_candidate``'s own readers. Spec 11's ``tiny_build`` has no enrichment
cache or gold yet, so the hand-built fixture of the unit tests stands in for it.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.compute as pc
import pytest
from tests.unit.enrich import _evaluate_fixtures as fx

from herness.enrich.calibrate import CalibrationStore, cross_fit
from herness.enrich.evaluate import evaluate_candidate, question_metrics
from herness.enrich.labels import gold_digest

pytestmark = pytest.mark.eval

_TOL = 0.005
_SPACES = {"root_cause": fx.CHOICE, "change_caused": ("true", "false")}


def _latest(table: pa.Table, qid: str, fp: str) -> dict[str, dict[str, float]]:
    """Latest distribution per content hash for one question at its fingerprint."""
    mask = pc.and_(pc.equal(table["question"], qid), pc.equal(table["question_fingerprint"], fp))
    rows = table.filter(mask).sort_by([("decided_at", "ascending")]).to_pylist()
    return {r["content_hash"]: dict(r["distribution"]) for r in rows}


def test_et03_02_eval_json_cross_check(tmp_path: Path) -> None:
    """ET03-02 recomputed metrics within 0.005 of eval.json; gold digest equal."""
    f = fx.build(tmp_path)
    evaluate_candidate(
        version=fx.VERSION, qs=f.qs, cfg=f.cfg, store=f.store, cache=f.cache,
        calibration=f.calibration, teacher=fx.TEACHER, paths=f.paths, now=fx.NOW,
    )  # fmt: skip
    doc = json.loads((f.paths.laya_dir(fx.VERSION) / "eval.json").read_text("utf-8"))
    gold = f.store.read("gold")
    dataset = f.cache.dataset()
    assert dataset is not None
    cache = dataset.to_table()
    laya_rows = cache.filter(pc.equal(cache["decider_version"], fx.VERSION))
    stored = CalibrationStore(f.paths).load("laya", fx.VERSION, fx.QSV)
    for qid, entry in doc["questions"].items():
        q = f.qs.get(qid)
        g = gold.filter(
            pc.and_(
                pc.equal(gold["question"], qid),
                pc.equal(gold["question_fingerprint"], q.fingerprint),
            )
        ).to_pylist()
        dists = _latest(laya_rows, qid, q.fingerprint)
        space = _SPACES[qid]
        probs = np.array([[dists[r["content_hash"]].get(a, 0.0) for a in space] for r in g])
        labels = np.array([space.index(r["answer"]) for r in g])
        folds = np.array([r["fold"] for r in g])
        cal = cross_fit(probs, labels, folds, q.type)
        m = question_metrics(probs, labels, folds, qtype=q.type, threshold=q.threshold)
        assert stored[qid].temperature == pytest.approx(cal.temperature, abs=_TOL)
        assert entry["n_gold"] == len(g)
        laya = entry["laya"]
        for name in ("accuracy", "ece", "temperature", "coverage_at_threshold"):
            assert laya[name] == pytest.approx(getattr(m, name), abs=_TOL), (qid, name)
        for name in ("macro_f1", "mae", "within_one", "accuracy_at_threshold"):
            expected = getattr(m, name)
            if expected is None:
                assert laya[name] is None, (qid, name)
            else:
                assert laya[name] == pytest.approx(expected, abs=_TOL), (qid, name)
    assert doc["gold_sha256"] == gold_digest(gold)  # spec 11: the whole gold directory
