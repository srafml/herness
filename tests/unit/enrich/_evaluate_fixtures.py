"""Hand-built cache + frozen gold for evaluation tests (UT03-125, ET03-02).

Spec 11's ``tiny_build`` carries no enrichment cache or gold yet, so the fixture is built here:
``root_cause`` (choice, 200 gold rows, calibrated), ``change_caused`` (bool, 60 gold rows, so
``uncalibrated``) and ``business_impact`` (score, gold not frozen, so absent from ``eval.json``).
"""

from __future__ import annotations

import dataclasses
import datetime
from pathlib import Path
from typing import Any

import pyarrow as pa
import yaml

from herness.core.types import QuestionSet
from herness.enrich.cache import CACHE_SCHEMA, DecisionCache, write_part
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.labels import GOLD_SCHEMA, LabelStore, gold_digest
from herness.enrich.layout import EnrichPaths
from herness.enrich.questions import load_question_set
from herness.enrich.settings import DecisionsConfig

QSV = "qs-2026-10-01.1"
VERSION = "laya-20261004-1"
TEACHER = ("openjev", "openjev-0.4.0/qwen:7b")
NOW = datetime.datetime(2026, 10, 4, 3, 12, tzinfo=datetime.UTC)
T0 = datetime.datetime(2026, 10, 1, tzinfo=datetime.UTC)
USER = "ab" * 16
CHOICE = ("software_defect", "config_change", "unknown")
N_CHOICE, N_BOOL = 200, 60

DECISIONS_YAML = """
question_set_version: qs-2026-10-01.1
primary_decider: laya
escalation_chain: [openjev, llm]
change_link: {use_decider: false}
questions:
  - id: root_cause
    type: choice
    applies_to: [incident]
    instructions: "Most likely root cause category of this IT incident, based on the description."
    options:
      software_defect: "application bug, code error, regression"
      config_change: "misconfiguration, bad parameter, config drift"
      unknown: "not enough information to tell"
    threshold: 0.70
  - id: change_caused
    type: bool
    applies_to: [incident]
    instructions: "The description states the issue started after a change."
    threshold: 0.80
  - id: business_impact
    type: score
    applies_to: [incident]
    instructions: "Business impact described in the text."
    levels: ["none or single user", "team or workaround", "department", "customer outage"]
    threshold: 0.60
"""


def decisions(**acceptance: Any) -> DecisionsConfig:
    """The config; ``acceptance`` overrides ``root_cause``'s criteria (not fingerprinted)."""
    raw: dict[str, Any] = yaml.safe_load(DECISIONS_YAML)
    if acceptance:
        raw["questions"][0]["acceptance"] = acceptance
    return DecisionsConfig.model_validate(raw)


def chash(i: int) -> str:
    return f"{i:032x}"


def _choice_dist(answer: str, conf: float) -> list[tuple[str, float]]:
    rest = (1.0 - conf) / 2
    return [(label, conf if label == answer else rest) for label in CHOICE]


def _bool_dist(answer: str, conf: float) -> list[tuple[str, float]]:
    other = "false" if answer == "true" else "true"
    return [(answer, conf), (other, 1.0 - conf)]


def gold_answer(qid: str, i: int) -> str:
    if qid == "root_cause":
        return CHOICE[i % 3]
    if qid == "change_caused":
        return "true" if i % 2 == 0 else "false"
    return str(i % 4)


def _flip(qid: str, answer: str) -> str:
    if qid == "change_caused":
        return "false" if answer == "true" else "true"
    return CHOICE[(CHOICE.index(answer) + 1) % 3]


def laya_row(qid: str, i: int) -> list[tuple[str, float]]:
    """Laya: choice 0.96, wrong on row 50 of every 100; bool right, 0.98 (every 20th: 0.6)."""
    answer = gold_answer(qid, i)
    if qid == "root_cause":
        return _choice_dist(_flip(qid, answer) if i % 100 == 50 else answer, 0.96)
    return _bool_dist(answer, 0.6 if i % 20 == 0 else 0.98)


def teacher_row(qid: str, i: int) -> list[tuple[str, float]]:
    """Teacher: wrong on every 10th choice row and every 20th bool row, confidence 0.9."""
    answer = gold_answer(qid, i)
    wrong = i % 10 == 3 if qid == "root_cause" else i % 20 == 0
    answer = _flip(qid, answer) if wrong else answer
    return _choice_dist(answer, 0.9) if qid == "root_cause" else _bool_dist(answer, 0.9)


def perfect_row(qid: str, i: int) -> list[tuple[str, float]]:
    """A teacher that is always right at 0.99."""
    answer = gold_answer(qid, i)
    return _choice_dist(answer, 0.99) if qid == "root_cause" else _bool_dist(answer, 0.99)


def _cache_rows(qs: QuestionSet, fn: Any, *, when: datetime.datetime) -> list[dict[str, Any]]:
    rows = []
    for qid, n in (("root_cause", N_CHOICE), ("change_caused", N_BOOL)):
        fp = qs.get(qid).fingerprint
        for i in range(n):
            dist = fn(qid, i)
            answer, prob = max(dist, key=lambda kv: kv[1])
            rows.append(
                {"content_hash": chash(i), "question": qid, "question_fingerprint": fp}
                | {"answer": answer, "probability": prob, "distribution": dist}
                | {"backend_confidence": None, "samples": None, "decided_at": when}
            )
    return rows


def _gold_rows(qid: str, fp: str, n: int) -> list[dict[str, Any]]:
    return [
        {"content_hash": chash(i), "record_id": f"INC-{i}", "question": qid}
        | {"question_fingerprint": fp, "answer": gold_answer(qid, i), "labeled_by": USER}
        | {"labeled_at": T0, "item_id": f"rev_{qid}_{i}", "fold": (i // 10) % 2}
        | {"adjudicated": False}
        for i in range(n)
    ]


@dataclasses.dataclass
class Fixture:
    paths: EnrichPaths
    cfg: DecisionsConfig
    qs: QuestionSet
    store: LabelStore
    cache: DecisionCache
    calibration: CalibrationStore
    frozen_gold: pa.Table


def build(tmp_path: Path) -> Fixture:
    """Write the cache parts and frozen gold under ``tmp_path/data``."""
    paths = EnrichPaths(
        data_root=tmp_path / "data", embedding_path="models/e", laya_current_file="c"
    )
    cfg = decisions()
    qs = load_question_set(cfg)
    store = LabelStore(paths, QSV)
    frozen = []
    for qid, n in (("root_cause", N_CHOICE), ("change_caused", N_BOOL), ("business_impact", 40)):
        fp = qs.get(qid).fingerprint
        table = pa.Table.from_pylist(_gold_rows(qid, fp, n), schema=GOLD_SCHEMA)
        store.append("gold", table)
        if qid != "business_impact":
            store.freeze_gold(qid, fp, gold_digest(table), n)
            frozen.append(table)
    stale = _gold_rows("root_cause", "0" * 16, 5)  # an older fingerprint: never evaluated
    store.append("gold", pa.Table.from_pylist(stale, schema=GOLD_SCHEMA))
    laya_part = paths.cache_partition(QSV, "laya", VERSION)
    teacher_part = paths.cache_partition(QSV, *TEACHER)
    write_part(laya_part, pa.Table.from_pylist(_cache_rows(qs, laya_row, when=T0), CACHE_SCHEMA))
    # an older duplicate that must lose to the latest row, and a stale-fingerprint row
    old = _cache_rows(qs, teacher_row, when=T0)[:1]
    old[0] |= {"distribution": _choice_dist("unknown", 0.99), "decided_at": T0.replace(day=1)}
    old.append(old[0] | {"question_fingerprint": "0" * 16, "decided_at": NOW})
    rows = _cache_rows(qs, teacher_row, when=T0.replace(day=2))
    write_part(teacher_part, pa.Table.from_pylist(old + rows, CACHE_SCHEMA))
    return Fixture(
        paths, cfg, qs, store, DecisionCache(paths, QSV), CalibrationStore(paths),
        pa.concat_tables(frozen),
    )  # fmt: skip


def write_teacher(f: Fixture, teacher: tuple[str, str], fn: Any) -> None:
    """Cache rows of another teacher partition."""
    part = f.paths.cache_partition(QSV, *teacher)
    write_part(part, pa.Table.from_pylist(_cache_rows(f.qs, fn, when=T0), CACHE_SCHEMA))
