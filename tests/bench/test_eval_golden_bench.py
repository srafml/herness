"""BT11-06: the mock golden eval on CPU stays under 300 s (impl 11 §10, T11-32).

Times one `herness eval --mock-llm tests/eval/mock_scripts` run in-process (`handle_eval`
on the `--inline` path, R-45; the CLI is T09-24's) over the committed suite on the seed 42
`tiny` scored build of `tests.support.eval_golden` (ET11-01's setup), and records the wall
time through the session `bench_recorder` (U11-51). The build itself is not timed. Held
bench: run it on an idle host (w30 rules).

Run: pytest -m slow tests/bench/test_eval_golden_bench.py
"""

from __future__ import annotations

import sys
from typing import Final

import pytest
from tests.support.bench import BenchRecorder, BenchTarget, record_value
from tests.support.eval_golden_run import cached_build, golden_payload, run_golden_job

pytestmark = [pytest.mark.integration, pytest.mark.slow]

LIMIT_S: Final = 300.0


def test_bt11_06_mock_golden_eval_under_300_s(
    tmp_path_factory: pytest.TempPathFactory, bench_recorder: BenchRecorder
) -> None:
    """BT11-06 mock golden eval on CPU, committed scripts, tiny build: timed run < 300 s."""
    build = cached_build(42, "tiny", tmp_path_factory)
    run = run_golden_job(build, tmp_path_factory.mktemp("bt11-06"), golden_payload())
    assert len(run.results) == 36
    sys.stderr.write(f"BT11-06 seconds {run.wall_s:.2f} s (target < 300)\n")
    target = BenchTarget("BT11-06", "11", "seconds", "s", LIMIT_S, "tiny")
    rec = record_value(bench_recorder, target, run.wall_s)
    assert rec.passed, f"BT11-06 {run.wall_s:.2f} s >= {LIMIT_S:g} s"
