"""ST11-05 (TH11-12): a number that did not come from SQL fails the eval (impl 11 §11.5).

A mock script whose chat answer for G01 says "about 57 incidents" outside a `[[n<k>]]`
marker. TH11-12 is a Verifier escape: the eval must catch the number even when the answer
got past the Verifier, because `count_unsupported` is independent of it. The real
`Verifier` stops such an answer (the chat service then answers "No verified answer could be
produced."), so the escape is simulated: the chat service's answer verifier passes every
answer. `count_unsupported` then counts the stray numeral: `unsupported_number_rate > 0`,
the `unsupported_numbers` threshold fails and the run exits 14. Integration: the real chat
service on the seed 42 `tiny` scored build, run in-process through `handle_eval` (the
`--inline` path, R-45). The script is the committed G01 chat script
(`tests/eval/mock_scripts/chat.yaml`) with the stray number put into its answer.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Final

import pytest
import yaml
from tests.support.eval_golden import MOCK_SCRIPTS
from tests.support.eval_golden_run import cached_build, golden_payload, run_golden_job

from herness.core.types import ChatAnswer, VerificationResult

pytestmark = pytest.mark.integration

STRAY: Final = "There were about 57 incidents; the build counts [[n1]] P1 incidents."
TURNS_PER_QUESTION: Final = 4


class EscapingVerifier:
    """An answer verifier that lets every answer through (the TH11-12 escape)."""

    def verify_answer(self, answer: ChatAnswer, build_id: str) -> VerificationResult:
        del answer
        return VerificationResult.model_validate(
            {"build_id": build_id, "passed": True, "items": [], "n_numbers": 0, "n_failed": 0,
             "verified_at": datetime.now(UTC), "duration_ms": 0}
        )  # fmt: skip


def _stray_script(target: Path) -> Path:
    """The committed G01 chat turns with the stray number in the final text and answer."""
    doc: list[dict[str, Any]] = yaml.safe_load((MOCK_SCRIPTS / "chat.yaml").read_text("utf-8"))
    script = doc[0]
    turns = script["turns"][:TURNS_PER_QUESTION]  # G01: tool call, text, answer, classifier
    turns[1] = {"final": {"text": STRAY}}
    turns[2] = {"final": {"output": {**turns[2]["final"]["output"], "text": STRAY}}}
    target.mkdir()
    out = [{"match": script["match"], "turns": turns}]
    (target / "chat.yaml").write_text(yaml.safe_dump(out, sort_keys=False), encoding="utf-8")
    return target


def test_st11_05_stray_number_in_chat_answer_fails_the_eval(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """ST11-05 chat answer "about 57 incidents" outside a marker: unsupported_number_rate > 0,
    the unsupported_numbers threshold fails, exit 14."""
    build = cached_build(42, "tiny", tmp_path_factory)
    scripts = _stray_script(tmp_path / "scripts")
    payload = golden_payload(scripts, ids=["G01"])
    run = run_golden_job(build, tmp_path / "work", payload, chat_verifier=EscapingVerifier())
    line = run.result("G01")
    assert "about 57 incidents" in line["final_text"]
    assert line["unsupported"]["stray_numerals"] == ["57"]
    assert line["unsupported"]["unsupported"] == 1
    assert run.summary["metrics"]["unsupported_number_rate"] > 0
    assert run.threshold("unsupported_numbers")["passed"] is False
    assert (run.exit_code, run.passed) == (14, False)


def test_st11_05_real_verifier_stops_the_stray_number(
    tmp_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> None:
    """ST11-05 without the escape the real Verifier stops the answer: the stray number never
    reaches the graded text (defence in depth; the question fails, exit 14)."""
    build = cached_build(42, "tiny", tmp_path_factory)
    scripts = tmp_path / "scripts"
    _stray_script(scripts)
    doc = yaml.safe_load((scripts / "chat.yaml").read_text("utf-8"))
    turns = doc[0]["turns"]
    doc[0]["turns"] = [*turns[:3], *turns[:3], turns[3]]  # the chat service retries once
    (scripts / "chat.yaml").write_text(yaml.safe_dump(doc, sort_keys=False), encoding="utf-8")
    run = run_golden_job(build, tmp_path / "work", golden_payload(scripts, ids=["G01"]))
    line = run.result("G01")
    assert "57" not in line["final_text"]
    assert line["status"] == "failed"
    assert run.exit_code == 14
