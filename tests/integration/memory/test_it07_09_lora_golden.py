"""IT07-09 (F07-11): the LoRA export excludes qa_pairs similar to `load_suite`'s golden
questions (T07-24).

The qa_pairs are grown by real runs: three funding reviews through the T06-22 driver (`drv`,
pinned to `tiny_build`) with the real spec 07 `MemoryStore`, whose record step promotes the two
SQL templates of `_it07_procedural` and moves their qa_pairs (one per task question and query
id) to `active`. The golden list is the questions of a test-local suite read by the real spec
11 `load_suite` (T11-32 owns the golden suite); the export goes through
`MemoryStore.export_lora`. The memory embedder is the deterministic fake: one golden question is
a paraphrase placed at cosine 0.95 to one run question (above the 0.90 cut), the other is
unrelated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from tests.integration.eval._eval_it_env import question
from tests.integration.harness import _swarm_run_env as e
from tests.integration.harness._swarm_run_env import Drv
from tests.integration.memory._it07_env import memory_store, pin_tiny, record_portfolio, run_review
from tests.integration.memory._it07_procedural import cite_literal_sql, template_rows
from tests.support.builds import BuildHandle
from tests.unit.eval._golden_fixtures import write_suite
from tests.unit.harness._blackboard_env import test_redactor
from tests.unit.harness.memory._write_env import at_cosine, unit

from herness.eval.golden import load_suite
from herness.store.ops import core

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "test_redactor"]

RUN_QUESTION = "Review the incident load of team t1 (1)."  # a stub-pipeline task objective
PARAPHRASE = "Assess how many incidents team t1 had (first)."
UNRELATED = "Which team owns the billing service?"


def _qa_pairs() -> list[dict[str, Any]]:
    rows = core.read_all(
        "SELECT memory_id, status, data FROM memory_item WHERE kind = 'qa_pair'", ()
    )
    return [dict(r) | {"data": core.load_json(r["data"], field="data")} for r in rows]


def _exported(out: Path) -> set[str]:
    return {
        json.loads(line)["id"]
        for name in ("train.jsonl", "val.jsonl")
        for line in (out / name).read_text(encoding="utf-8").splitlines()
        if line
    }


def test_it07_09_golden_questions_from_load_suite_exclude_similar_qa_pairs(
    drv: Drv, tiny_build: BuildHandle, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IT07-09 `load_suite` golden questions: the qa_pairs whose question is similar to a
    golden question (cosine >= 0.90) are excluded from the export; the others are exported."""
    data_root = Path(drv.hcfg.paths.data)
    pin_tiny(drv, data_root, tiny_build, monkeypatch)
    record_portfolio()
    cite_literal_sql(drv, monkeypatch)
    mem = memory_store(tmp_path, data_root)
    mem.embed.overrides.update({RUN_QUESTION: unit(0), PARAPHRASE: at_cosine(0.95)})
    for _ in range(3):
        assert run_review(drv, mem.store).status == "done"
    assert [t["status"] for t in template_rows()] == ["active", "active"]
    pairs = [p for p in _qa_pairs() if p["status"] == "active"]
    similar = {p["memory_id"] for p in pairs if p["data"]["question"] == RUN_QUESTION}
    assert similar
    assert len(similar) < len(pairs)
    suite = load_suite(write_suite(tmp_path / "golden.yaml", [
        question("G01", question=PARAPHRASE), question("G02", question=UNRELATED),
    ]))  # fmt: skip
    golden = [q.question for q in suite.questions]
    assert golden == [PARAPHRASE, UNRELATED]

    report = mem.store.export_lora(data_root / "models" / "lora_data", min_pass_lb=0.7,
                                   golden_questions=golden)  # fmt: skip

    exported = _exported(report.out_dir)
    assert report.excluded_golden == len(similar)
    assert exported.isdisjoint(similar)  # similar questions excluded
    assert exported == {p["memory_id"] for p in pairs} - similar
    manifest = json.loads((report.out_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["excluded_golden"] == len(similar)
    assert {PARAPHRASE, UNRELATED} <= set(mem.embed.calls)  # the suite is the golden source
