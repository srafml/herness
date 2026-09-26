"""Laya fine-tune benchmark (impl 03 BT03-12, T03-31).

Run: pytest -m "integration and slow" tests/bench/enrich. Needs the real `laya` package,
CUDA and a base Laya directory in `HERNESS_BT03_12_BASE_DIR`; skipped otherwise.
"""

from __future__ import annotations

import importlib.util
import os
import time
from pathlib import Path
from typing import Any, cast

import pyarrow as pa
import pytest
from structlog.testing import capture_logs

from herness.core.jobs import JobContext
from herness.core.types import Question, QuestionSet
from herness.enrich.labels import HUMAN_SCHEMA, TEACHER_SCHEMA
from herness.enrich.laya_trainer import SoftLabelSftTrainer, TrainHyper, build_training_set
from herness.enrich.questions import question_fingerprint

pytestmark = [pytest.mark.integration, pytest.mark.slow, pytest.mark.gpu]

_RECORDS = 30_000
_BUDGET_S = 6 * 3600.0


def _laya_on_cuda() -> bool:
    if importlib.util.find_spec("laya") is None:
        return False
    import torch  # noqa: PLC0415 - only when laya is importable

    return torch.cuda.is_available()


class _Ctx:
    def should_yield(self) -> bool:
        return False

    def heartbeat(self, note: str | None = None) -> None:
        return None


@pytest.mark.skipif(not _laya_on_cuda(), reason="needs the laya package and CUDA")
def test_bt03_12_laya_fine_tune_30k_records_within_6h(tmp_path: Path) -> None:
    """BT03-12 SFT fine-tune over 30k records on one 24 GB card finishes within 6 h."""
    base = os.environ.get("HERNESS_BT03_12_BASE_DIR")
    if not base:
        pytest.skip("HERNESS_BT03_12_BASE_DIR not set")
    question = Question(
        id="root_cause", type="choice", instructions="Pick the root cause category.",
        options={"network": "Network fault", "disk": "Disk fault", "code": "Code defect"},
        threshold=0.7,
    )  # fmt: skip
    qs = QuestionSet(version="qs-2026-10-01.1", questions=(question,))
    labels = ["network", "disk", "code"]
    rows: list[dict[str, Any]] = []
    for i in range(_RECORDS):
        top = labels[i % 3]
        rows.append({
            "content_hash": f"{i:032x}", "record_id": f"r{i}", "question": question.id,
            "question_fingerprint": question_fingerprint(question), "answer": top,
            "distribution": [(label, 0.8 if label == top else 0.1) for label in labels],
            "decider": "openjev", "decider_version": "openjev-0.4.0", "round": 1,
            "stratum": "s", "purpose": "sample",
        })  # fmt: skip
    texts = {f"{i:032x}": f"synthetic ticket {i} " * 20 for i in range(_RECORDS)}
    data = build_training_set(
        pa.Table.from_pylist(rows, schema=TEACHER_SCHEMA), HUMAN_SCHEMA.empty_table(),
        texts=texts, questions=qs, gold=set(),
    )  # fmt: skip
    started = time.monotonic()
    with capture_logs() as logs:
        result = SoftLabelSftTrainer().train(
            data, init_dir=Path(base), out_dir=tmp_path, hyper=TrainHyper(seed=7),
            ctx=cast(JobContext, _Ctx()),
        )  # fmt: skip
    assert result.epochs_run >= 1
    assert time.monotonic() - started <= _BUDGET_S
    assert not [e for e in logs if e["event"] == "enrich.distill.wall_clock_cap"]  # not capped
