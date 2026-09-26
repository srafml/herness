"""IT03-16 (U03-58): Laya `fast` on/off parity on a real GPU (T03-14).

Runs only on the dev box: needs the `laya` package (V-11, not a dependency yet), CUDA,
and `HERNESS_IT_LAYA_DATA` pointing at a data root whose `models/laya/CURRENT` names a
verified version. Required before `deciders.laya.fast` is enabled (design 03 §3.3, §10).
"""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path

import pytest

from herness.core.types import DecisionInput, Question, QuestionSet
from herness.enrich.deciders.laya import LayaDecider
from herness.enrich.layout import EnrichPaths
from herness.enrich.settings import LayaSettings

pytestmark = [pytest.mark.integration, pytest.mark.gpu, pytest.mark.slow]

_DATA_ENV = "HERNESS_IT_LAYA_DATA"
_N_STATES = 1000
_MIN_AGREEMENT = 0.98
_INSTR = "Classify the incident ticket text."
_QS = QuestionSet(
    version="qs-2026-10-01.1",
    questions=(
        Question(id="is_outage", type="bool", instructions=_INSTR, threshold=0.8),
        Question(
            id="severity",
            type="choice",
            instructions=_INSTR,
            options={"low": "Low impact", "mid": "Medium impact", "high": "High impact"},
            threshold=0.8,
        ),
        Question(
            id="urgency",
            type="score",
            instructions=_INSTR,
            levels=("none", "some", "high", "critical"),
            threshold=0.8,
        ),
    ),
)
_SYMPTOMS = ("login fails", "disk full", "slow response", "service down", "printer jam")
_SYSTEMS = ("payroll", "email", "vpn", "billing api", "warehouse db")


def _environment_ready() -> bool:
    if importlib.util.find_spec("laya") is None or not os.environ.get(_DATA_ENV):
        return False
    import torch  # noqa: PLC0415 - only probed once the other preconditions hold

    return bool(torch.cuda.is_available())


def _states() -> list[DecisionInput]:
    items = []
    for i in range(_N_STATES):
        symptom, system = _SYMPTOMS[i % len(_SYMPTOMS)], _SYSTEMS[(i // 5) % len(_SYSTEMS)]
        text = f"Ticket {i}: users report {symptom} on {system} since {i % 24}:00."
        items.append(
            DecisionInput(
                record_id=f"INC{i:06d}", entity="incident", content_hash=f"{i:032x}", text=text
            )
        )
    return items


@pytest.mark.skipif(not _environment_ready(), reason="needs laya, CUDA and HERNESS_IT_LAYA_DATA")
def test_it03_16_fast_path_argmax_parity() -> None:
    """IT03-16 real Laya on GPU, fast on/off, 1k states: argmax agreement >= 98 %."""
    paths = EnrichPaths(
        data_root=Path(os.environ[_DATA_ENV]).resolve(),
        embedding_path="data/models/embedding",
        laya_current_file="data/models/laya/CURRENT",
    )
    items = _states()
    answers = {}
    for fast in (False, True):
        decider = LayaDecider(LayaSettings(device="cuda", fast=fast), paths=paths)
        decider.load()
        try:
            answers[fast] = decider.decide(items, _QS)
        finally:
            decider.unload()
    pairs = [
        (slow.answers[qid].answer, quick.answers[qid].answer)
        for slow, quick in zip(answers[False], answers[True], strict=True)
        for qid in slow.answers
    ]
    assert len(pairs) == _N_STATES * len(_QS.questions)
    agreement = sum(a == b for a, b in pairs) / len(pairs)
    assert agreement >= _MIN_AGREEMENT
