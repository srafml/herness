"""Security test of the classifier eval's gold hash (T11-35): ST11-04 (TH11-03).

The gold set is what the Laya gate was measured on; editing it to hide a regression must be
caught. `run_classifier` recomputes `gold_sha256` over the gold directory with impl 03's
`gold_digest` and compares it with `eval.json`; a mismatch, or a part that no longer reads,
is a failed check, so the eval exits 14 (never a silent pass, never a crash). Runs on the
IT11-25 fixture model directory (`tests/unit/eval/_classifier_env.py`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from tests.unit.eval._classifier_env import QSV, ClassifierEnv, classifier_env

from herness.eval.runner import exit_code

pytestmark = pytest.mark.integration
__all__ = ["classifier_env"]  # fixture


def _gold_parts(env: ClassifierEnv) -> list[Path]:
    return sorted(env.paths.labels_dir(QSV, "gold").glob("part-*.parquet"))


def _gold_check(env: ClassifierEnv, run_id: object) -> dict[str, object]:
    report = json.loads((env.out_dir(run_id) / "classifier.json").read_text(encoding="utf-8"))
    return next(c for c in report["gate_check"] if c["check"] == "gold_sha256")


def test_st11_04_one_flipped_byte_fails_the_gold_hash(classifier_env: ClassifierEnv) -> None:
    """ST11-04 modify one byte of a gold part in the IT11-25 fixture: gold hash check fails;
    exit 14 (integration)."""
    part = _gold_parts(classifier_env)[0]
    data = bytearray(part.read_bytes())
    data[len(data) // 3] ^= 0x01
    part.write_bytes(bytes(data))
    _, outcome = classifier_env.handle()
    assert outcome.result["passed"] is False
    assert exit_code(outcome.model_dump()) == 14
    check = _gold_check(classifier_env, outcome.result["run_id"])
    assert check["passed"] is False
    assert check["recomputed"] != check["stored"]


def test_st11_04_edited_gold_answer_fails_the_gold_hash(classifier_env: ClassifierEnv) -> None:
    """ST11-04 a well-formed gold part with one answer changed (the regression-hiding edit):
    the digest differs and the eval exits 14."""
    part = _gold_parts(classifier_env)[0]
    table = pq.read_table(part)
    answers = table.column("answer").to_pylist()
    answers[0] = "unknown" if answers[0] != "unknown" else "software_defect"
    index = table.schema.get_field_index("answer")
    edited = table.set_column(index, "answer", pa.array(answers, type=pa.string()))
    pq.write_table(edited, part, compression="zstd")
    _, outcome = classifier_env.handle()
    assert exit_code(outcome.model_dump()) == 14
    check = _gold_check(classifier_env, outcome.result["run_id"])
    assert check["passed"] is False
    assert isinstance(check["recomputed"], str)
    assert check["recomputed"] != check["stored"]
