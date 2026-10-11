"""IT11-32: a Writer-dead review graded from its verified findings (F11-07, U11-62, R-49).

`herness eval --inline --mock-llm tests/eval/mock_scripts_writer_dead --ids O01` runs
in-process (`handle_eval`, the `--inline` path, R-45; the CLI is T09-24's) on the seed 42
`tiny` scored build of `tests.support.eval_golden`. The org review is a real swarm run whose
Writer calls all fault with `http_500` (`at: 0`, `count: 100`): the Writer task dies, the
run ends `partial` with a `findings_only` draft, and the eval grades O01 from the verified
findings. The committed fixture draft `tests/fixtures/drafts/findings_only.json` (UT11-114)
is checked against the shape the swarm really writes (U11-75).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Final

import pytest
from tests.support.eval_golden import REPO, WRITER_DEAD_SCRIPTS
from tests.support.eval_golden_run import GoldenRun, cached_build, golden_payload, run_golden_job

from herness.core.types import ReportDraft
from herness.eval.scripted import load_scripts

pytestmark = pytest.mark.integration

FIXTURE_DRAFT: Final = REPO / "tests" / "fixtures" / "drafts" / "findings_only.json"


@pytest.fixture(scope="module")
def writer_dead(tmp_path_factory: pytest.TempPathFactory) -> tuple[GoldenRun, ReportDraft, Path]:
    build = cached_build(42, "tiny", tmp_path_factory)
    work = tmp_path_factory.mktemp("it11-32")
    payload = golden_payload(WRITER_DEAD_SCRIPTS, ids=["O01"])
    run = run_golden_job(build, work, payload)
    review_run = run.result("O01")["pipeline_run_id"]
    draft_file = work / "data" / "reports" / str(review_run) / "draft.json"
    draft = ReportDraft.model_validate_json(draft_file.read_bytes())
    return run, draft, work


def _review_status(work: Path, run_id: str) -> str:
    import sqlite3  # noqa: PLC0415 - read-only look at the copied ops store

    con = sqlite3.connect(f"file:{work / 'data' / 'ops.sqlite'}?mode=ro", uri=True)
    try:
        row = con.execute("SELECT status FROM run WHERE run_id = ?", [run_id]).fetchone()
    finally:
        con.close()
    return str(row[0])


def test_it11_32_writer_dead_run_is_partial_with_findings_only_draft(
    writer_dead: tuple[GoldenRun, ReportDraft, Path],
) -> None:
    """IT11-32 the review run is `partial` with a `findings_only` draft; the results line has
    `draft_mode = "findings_only"`."""
    run, draft, work = writer_dead
    line = run.result("O01")
    assert [r["id"] for r in run.results] == ["O01"]
    assert line["draft_mode"] == "findings_only"
    assert draft.mode == "findings_only"
    assert _review_status(work, line["pipeline_run_id"]) == "partial"
    assert "partial_run" in draft.banners
    assert draft.coverage.publishable is False


def test_it11_32_o01_graded_from_verified_findings(
    writer_dead: tuple[GoldenRun, ReportDraft, Path],
) -> None:
    """IT11-32 O01's entity check is graded from the verified findings' draft (rank 1 is the
    planted T1 team) and every number the answer cites is supported: unsupported_number_rate
    is 0."""
    run, draft, _work = writer_dead
    line = run.result("O01")
    checks = {c["check"]: c for c in line["checks"]}
    assert checks["entities:rank1"]["status"] == "passed"
    assert line["status"] == "passed"
    claims = [p.text for s in draft.sections for p in s.paragraphs]
    assert claims  # the verified findings, one paragraph each
    assert all(claim in line["final_text"] for claim in claims)
    assert line["numbers"]  # the findings' NumberRefs are the carriers
    assert line["unsupported"]["unsupported"] == 0
    assert run.summary["metrics"]["unsupported_number_rate"] == 0


def test_it11_32_writer_script_faults_every_call() -> None:
    """IT11-32 the Writer-dead set replaces the Writer script with one whose every call faults
    with `http_500` (`at: 0`, `count: 100`); the other review roles are the committed ones."""
    book = load_scripts(WRITER_DEAD_SCRIPTS)
    writer = next(s for s in book.scripts if s.match.role == "writer")
    assert [(f.at, f.kind, f.count) for f in writer.faults] == [(0, "http_500", 100)]
    roles = {s.match.role for s in book.scripts}
    assert {"planner", "skeptic", "analyst_ops", "analyst_org"} <= roles


def _shape(draft: dict[str, Any]) -> dict[str, Any]:
    return {
        "mode": draft["mode"],
        "sections": [(s["id"], s["title"]) for s in draft["sections"]],
        "recommendations": draft["recommendations"],
        "partial_run": "partial_run" in draft["banners"],
        "publishable": draft["coverage"]["publishable"],
        "one_finding_per_paragraph": all(
            len(p["finding_ids"]) == 1 for s in draft["sections"] for p in s["paragraphs"]
        ),
    }


def test_it11_32_fixture_draft_has_the_real_findings_only_shape(
    writer_dead: tuple[GoldenRun, ReportDraft, Path],
) -> None:
    """IT11-32 the committed `findings_only.json` (U11-75; UT11-114) has the shape the swarm
    writes for a dead Writer: mode `findings_only`, one `Verified findings` section with one
    verified finding per paragraph, no recommendation, `partial_run`, not publishable; the
    fixture holds two verified findings."""
    _run, draft, _work = writer_dead
    fixture = json.loads(FIXTURE_DRAFT.read_text(encoding="utf-8"))
    ReportDraft.model_validate(fixture)
    assert _shape(fixture) == _shape(draft.model_dump(mode="json"))
    assert sum(len(s["paragraphs"]) for s in fixture["sections"]) == 2
