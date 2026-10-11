"""Integration check (T11-38) of `herness eval --compare PROFILE` end to end on `tiny_build`.

`handle_eval` runs in-process (the `--inline` path, R-45) with the test-local mock setup of
`_eval_it_env` (real swarm driver with scripted agents, scripted chat, a one-script mock
directory): the suite runs under `synth` and again under `local` on the same pinned build,
and `compare` reads only the two stored runs. Identical scripted numbers → no harness defect.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env, question
from tests.integration.harness import _swarm_run_env as e
from tests.unit.eval._runner_env import settings
from tests.unit.harness._blackboard_env import test_redactor

from herness.eval.runner import build_eval_payload, exit_code
from herness.store.ops import get_run
from herness.store.ops.core import read_all

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]


def _payload(suite: object, mock: object, **extra: Any) -> dict[str, Any]:
    return build_eval_payload(
        suite=str(suite), profile="synth", compare_profile="local", depth="fast",
        ids=["G01", "F01", "O01"], no_judge=True, mock_llm_dir=str(mock), settings=settings(),
        **extra,
    )  # fmt: skip


def test_cv_t11_38_compare_end_to_end_on_tiny_build(eval_env: EvalEnv) -> None:
    """CV-T11-38 `--compare local` on tiny_build with mock scripts: two finished runs on the
    same build, `comparison.json` and `comparison.md` in the newer run, identical numbers
    (no harness defect, no flip) and exit 0."""
    suite = eval_env.suite([question("G01"), question("F01", "funding"), question("O01", "org")])
    mock = eval_env.tmp / "mock"
    mock.mkdir()
    (mock / "judge.yaml").write_text(
        "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n", encoding="utf-8"
    )
    _, outcome = eval_env.handle(_payload(suite, mock))

    assert outcome.status == "done"
    assert exit_code(outcome.model_dump()) == 0
    primary, second = outcome.result["run_id"], outcome.result["compare_run_id"]
    rows = read_all("SELECT run_id, profile, build_id FROM run WHERE kind = 'eval' "
                    "ORDER BY started_at", ())  # fmt: skip
    assert [(r["run_id"], r["profile"]) for r in rows] == [(primary, "synth"), (second, "local")]
    assert {r["build_id"] for r in rows} == {eval_env.build.build_id}
    first = get_run(str(primary))
    assert first is not None
    assert first.meta["compare_profile"] == "local"
    out = eval_env.out_dir(second)
    table = json.loads((out / "comparison.json").read_text(encoding="utf-8"))
    assert table["run_ids"] == [primary, second]
    assert [(r["profile"], r["depth"]) for r in table["rows"]] == [("synth", "fast"),
                                                                   ("local", "fast")]  # fmt: skip
    assert table["harness_defects"] == []
    assert table["flips"] == []
    assert table["suite_changed"] is False
    assert table["deltas"][0]["metrics"]["answer_correctness"] == 0.0
    assert (out / "comparison.md").is_file()
    assert "## Comparison" in (out / "report.md").read_text(encoding="utf-8")
