"""Integration tests of change linking (F03-05 stage level, F03-11; impl 03 T03-26).

Spec 11's synthetic truth T3, ``tiny_build`` and ``StubDeciderServer`` are not in the tree yet:
IT03-13 builds a seeded synthetic incident/change set with known causal links here, and IT03-09
runs the pair flow at stage level with an in-process fake decider (carry-overs in the T03-26
report). ``core.incident`` and ``core.change`` are hand-built with the columns the stage reads.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

import duckdb
import pytest

from herness.core.types import Answer, DecisionInput, DecisionOutput, QuestionSet
from herness.enrich.cache import DecisionCache
from herness.enrich.calibrate import CalibrationStore
from herness.enrich.layout import EnrichPaths
from herness.enrich.link_changes import link_candidates, pair_inputs, run_link_stage
from herness.enrich.questions import load_question_set
from herness.enrich.settings import DecisionsConfig
from herness.enrich.text import content_hash

pytestmark = pytest.mark.integration

SETTINGS_SQL = Path(__file__).resolve().parents[3] / "herness/model/sql/000_settings.sql"
T0 = datetime(2026, 7, 1, tzinfo=UTC)
QSV = "qs-2026-09-01"
PAIR_Q = {
    "id": "change_caused_pair",
    "type": "bool",
    "applies_to": ["incident"],
    "instructions": "The change described caused the incident described.",
    "threshold": 0.70,
    "scoring_use": False,
}


@dataclass
class Report:
    rows: int = 0
    decided: int = 0


def _cfg(**change_link: Any) -> DecisionsConfig:
    return DecisionsConfig.model_validate(
        {"question_set_version": QSV, "questions": [PAIR_Q], "change_link": change_link}
    )


def _warehouse(
    incidents: Sequence[tuple[Any, ...]],
    changes: Sequence[tuple[Any, ...]],
    texts: Sequence[tuple[str, str, str]] = (),
) -> duckdb.DuckDBPyConnection:
    wh = duckdb.connect()
    wh.execute(SETTINGS_SQL.read_text("utf-8"))
    wh.execute(
        "CREATE TABLE core.incident (record_id VARCHAR, opened_at TIMESTAMPTZ, ci_id VARCHAR, "
        "service_id VARCHAR, caused_by_change_id VARCHAR)"
    )
    wh.execute(
        "CREATE TABLE core.change (record_id VARCHAR, type VARCHAR, planned_end TIMESTAMPTZ, "
        "actual_start TIMESTAMPTZ, actual_end TIMESTAMPTZ, ci_id VARCHAR, service_id VARCHAR, "
        "outcome VARCHAR)"
    )
    wh.executemany("INSERT INTO core.incident VALUES (?, ?, ?, ?, ?)", incidents)
    wh.executemany("INSERT INTO core.change VALUES (?, ?, ?, ?, ?, ?, ?, ?)", changes)
    for rid, entity, text in texts:
        wh.execute(
            "INSERT INTO enrich.text_redacted VALUES (?, ?, ?, ?)",
            [rid, entity, text, content_hash(text)],
        )
    return wh


# --- IT03-13 -----------------------------------------------------------------------------------


@dataclass
class _Synthetic:
    incidents: list[tuple[Any, ...]]
    changes: list[tuple[Any, ...]]
    truth: set[tuple[str, str]]


def _synthetic(seed: int) -> _Synthetic:
    """Seeded incidents/changes over 90 days, 12 services x 4 CIs, with known causal links.

    40 % of changes cause 1-3 incidents opened 0-8 h after the change on the same CI (a third
    of them carry ``caused_by_change_id``, a fifth only share the service and open within 90
    minutes, and 12 % surface 12-36 h later, beyond a 0.5 heuristic score); background
    incidents open at random times on random CIs.
    """
    rng = random.Random(seed)
    services = [f"svc{s}" for s in range(12)]
    cis = {s: [f"{s}-ci{c}" for c in range(4)] for s in services}
    horizon_h = 90 * 24
    incidents: list[tuple[Any, ...]] = []
    changes: list[tuple[Any, ...]] = []
    truth: set[tuple[str, str]] = set()
    for n in range(240):
        change_id, service = f"chg{n:04d}", rng.choice(services)
        ci = rng.choice(cis[service])
        end = T0 + timedelta(hours=rng.uniform(0, horizon_h))
        bad = rng.random() < 0.4
        outcome = rng.choice(["backed_out", "unsuccessful", "successful"]) if bad else "successful"
        kind = rng.choice(["normal", "normal", "emergency", "standard"])
        changes.append((change_id, kind, end, end - timedelta(hours=1), end, ci, service, outcome))
        for k in range(rng.randint(1, 3) if bad else 0):
            incident_id = f"inc-{change_id}-{k}"
            roll = rng.random()
            service_only = roll < 0.2
            late = rng.random() < 0.12
            delay = rng.uniform(12, 36) if late else rng.uniform(0, 1.5 if service_only else 8)
            field = change_id if roll > 0.67 else None
            inc_ci = None if service_only else ci
            opened = end + timedelta(hours=delay)
            incidents.append((incident_id, opened, inc_ci, service, field))
            truth.add((incident_id, change_id))
    for n in range(400):
        service = rng.choice(services)
        opened = T0 + timedelta(hours=rng.uniform(0, horizon_h))
        incidents.append((f"bg{n:04d}", opened, rng.choice(cis[service]), service, None))
    return _Synthetic(incidents, changes, truth)


@pytest.mark.parametrize("seed", [3, 17, 2026])
def test_it03_13_link_precision_and_recall_on_synthetic_truth(seed: int, tmp_path: Path) -> None:
    """IT03-13 synthetic causal truth: precision >= 0.80, recall >= 0.70 at score >= 0.5."""
    data = _synthetic(seed)
    wh = _warehouse(data.incidents, data.changes)
    cfg = _cfg(use_decider=False)
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    link_candidates(wh, cfg=cfg)
    run_link_stage(
        wh,
        cfg=cfg,
        qs=QuestionSet(version=QSV, questions=()),
        cache=DecisionCache(paths, QSV),
        calibration=CalibrationStore(paths),
        pair_decider=None,
        report=Report(),
    )
    rows = wh.execute(
        "SELECT incident_id, change_id FROM enrich.incident_change_link WHERE score >= 0.5"
    ).fetchall()
    predicted = set(rows)
    assert len(predicted) == len(rows)  # one row per (incident, change)
    hits = len(predicted & data.truth)
    precision, recall = hits / len(predicted), hits / len(data.truth)
    assert precision >= 0.80, (precision, recall)
    assert recall >= 0.70, (precision, recall)
    per_incident = wh.execute(
        "SELECT max(n) FROM (SELECT count(*) AS n FROM enrich.incident_change_link "
        "GROUP BY incident_id)"
    ).fetchone()
    assert per_incident is not None
    assert per_incident[0] <= 3


# --- IT03-09 -----------------------------------------------------------------------------------


class _FakePairDecider:
    """In-process stand-in for the stub decider: P(true) 0.9 for even changes, else 0.1."""

    name: Literal["openjev"] = "openjev"
    version = "openjev-0.4.0/stub:1"

    def decide(self, items: Sequence[DecisionInput], questions: QuestionSet) -> list[Any]:
        outputs = []
        for item in items:
            qid = questions.questions[0].id
            p = 0.9 if int(item.record_id[-1]) % 2 == 0 else 0.1
            answer = Answer(
                answer="true" if p > 0.5 else "false",
                probability=max(p, 1 - p),
                distribution={"true": p, "false": round(1 - p, 6)},
            )
            outputs.append(
                DecisionOutput(
                    record_id=item.record_id,
                    content_hash=item.content_hash,
                    decider=self.name,
                    decider_version=self.version,
                    answers={qid: answer},
                )
            )
        return outputs


def test_it03_09_pair_decisions_cached_links_decider_not_in_decision(tmp_path: Path) -> None:
    """IT03-09 use_decider with a stub decider: pairs cached, method decider, no enrich.decision."""
    incidents = [(f"inc{i}", T0 + timedelta(days=i), None, "svc", None) for i in range(3)]
    changes = [
        (f"chg{i}{j}", "normal", None, None, T0 + timedelta(days=i, hours=-(j + 1)), None, "svc",
         "successful")
        for i in range(3)
        for j in range(2)
    ]  # fmt: skip
    texts = [(i[0], "incident", f"incident text {i[0]}") for i in incidents]
    texts += [(c[0], "change", f"change text {c[0]}") for c in changes]
    wh = _warehouse(incidents, changes, texts)
    cfg = _cfg(use_decider=True)
    qs = load_question_set(cfg)
    paths = EnrichPaths(data_root=tmp_path, embedding_path="data/e", laya_current_file="data/c")
    decider = _FakePairDecider()

    link_candidates(wh, cfg=cfg)
    items = pair_inputs(wh, cfg=cfg, qs=qs, paths=paths, build_id="build-1")
    assert len(items) == 6
    cache = DecisionCache(paths, QSV)
    with cache.writer(decider.name, decider.version, questions=qs, flush_rows=50) as writer:
        writer.add(decider.decide(items, qs), samples=None)
    report = Report()
    run_link_stage(
        wh,
        cfg=cfg,
        qs=qs,
        cache=cache,
        calibration=CalibrationStore(paths),
        pair_decider=(decider.name, decider.version),
        report=report,
    )

    cached = cache.existing_keys(decider.name, decider.version, qs)
    assert cached == {(item.content_hash, "change_caused_pair") for item in items}
    rows = wh.execute(
        "SELECT incident_id, change_id, method, score FROM enrich.incident_change_link"
    ).fetchall()
    assert len(rows) == 6
    assert {method for *_, method, _ in rows} == {"decider"}
    by_pair = {(i, c): s for i, c, _, s in rows}
    assert by_pair["inc0", "chg00"] > by_pair["inc0", "chg01"]  # the answer moves the score
    assert report.decided == 6
    assert wh.execute("SELECT count(*) FROM enrich.decision").fetchone() == (0,)
    assert (paths.pairs_dir() / "part-build-1.parquet").is_file()
