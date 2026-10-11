"""Make or check a mock golden baseline (impl 11 U11-75, U11-72; T11-32).

    uv run python -m tests.support.eval_golden_baseline --seed 42 --scale small \
        --set-baseline synthetic-42-small-mock [--exclude F05] [--work DIR]
    uv run python -m tests.support.eval_golden_baseline --seed 42 --scale small \
        --baseline synthetic-42-small-mock [--exclude F05]

builds the scored synthetic build (`eval_golden.scored_build`: generation, the real build,
truth-planted enrichment, real scoring) under `--work` (default: a new temp dir), then runs
one `eval` job in-process (`handle_eval`, the `--inline` path, R-45) over the committed suite
with `--mock-llm tests/eval/mock_scripts`, profile `local`, depth `standard`, and either
saves the baseline into the checkout's `tests/eval/baselines/` (`--set-baseline`, synthetic
only, `-mock` names for mock runs) or compares against it (`--baseline`). `--exclude` drops
question ids (F05 waits for `seed_prior_run`, T11-33). Writes one JSON line (run id, exit
code, passed, thresholds, wall time) and exits with the job's U11-69 exit code.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from tests.support.eval_golden import REPO, SUITE
from tests.support.eval_golden_run import golden_payload, run_golden_job

from herness.eval.golden import load_suite


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m tests.support.eval_golden_baseline")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument("--scale", default="small", choices=["tiny", "small"])
    target = p.add_mutually_exclusive_group(required=True)
    target.add_argument("--set-baseline")
    target.add_argument("--baseline")
    p.add_argument("--exclude", default="", help="comma-separated question ids to leave out")
    p.add_argument("--work", type=Path, default=None)
    return p


def main(argv: Sequence[str] | None = None) -> int:
    """Build, run and write the JSON line; the job's exit code."""
    from tests.support.eval_golden import scored_build  # noqa: PLC0415 - heavy imports

    args = _parser().parse_args(argv)
    work = args.work or Path(tempfile.mkdtemp(prefix="golden-baseline-"))
    excluded = {q for q in args.exclude.split(",") if q}
    ids = [q.id for q in load_suite(SUITE).questions if q.id not in excluded]
    build = scored_build(args.seed, args.scale, work / "build")
    extra = (
        {"set_baseline": args.set_baseline} if args.set_baseline else {"baseline": args.baseline}
    )
    payload = golden_payload(ids=ids, **extra)
    run = run_golden_job(build, work / "run", payload, repo_root=REPO)
    line = {
        "run_id": run.run_id, "exit_code": run.exit_code, "passed": run.passed,
        "build_id": build.build_id, "wall_s": round(run.wall_s, 1), "excluded": sorted(excluded),
        "thresholds": {t["check"]: [t["passed"], t["detail"]] for t in run.summary["thresholds"]},
        "baseline": run.summary.get("baseline"), "out_dir": str(run.out_dir),
    }  # fmt: skip
    sys.stdout.write(json.dumps(line) + "\n")
    return run.exit_code


if __name__ == "__main__":
    sys.exit(main())
