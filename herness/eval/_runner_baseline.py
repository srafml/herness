"""Baseline choice of a golden eval run (F11-07 step 11; U11-72, T11-31 note 7).

Private sibling of `herness.eval.runner` (§2 module map), split off for the line budgets of
`runner.py` and `_runner_finish.py`. A baseline applies only when it is comparable with the
run: same profile and depth, a synthetic run, and `-mock` baselines for mock runs only. A
non-comparable explicit `--baseline` is `ConfigError`; a non-comparable `eval.baseline`
default counts as no baseline, with a visible note on the baseline-relative threshold rows.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from herness.core.errors import ConfigError
from herness.eval.report import baseline_mismatch, read_baseline

if TYPE_CHECKING:
    from pathlib import Path

    from herness.eval.metrics import EvalMetrics

__all__ = ["BaselineChoice", "resolve_baseline"]


@dataclass(frozen=True)
class BaselineChoice:
    """The baseline a run compares with: its name and metrics, or None with a `note` for the
    baseline-relative threshold rows when the default is not comparable (T11-31 note 7)."""

    name: str | None = None
    metrics: EvalMetrics | None = None
    note: str | None = None


def resolve_baseline(
    named: str | None, default: str, *, repo_root: Path, suite_sha256: str,
    run: tuple[str, str, str, bool],
) -> BaselineChoice:  # fmt: skip
    """F11-07 step 11: `--baseline NAME` (missing or not comparable → `ConfigError`), else the
    `eval.baseline` default (missing → none; not comparable → none with a visible note).
    `run` is (profile, depth, dataset_kind, mock)."""
    name = named if named is not None else default
    found = read_baseline(name, repo_root=repo_root, suite_sha256=suite_sha256)
    if found is None:
        if named is not None:
            msg = f"baseline not found: name={named}"
            raise ConfigError(msg)
        return BaselineChoice()
    profile, depth, kind, mock = run
    reason = baseline_mismatch(found, profile=profile, depth=depth, dataset_kind=kind, mock=mock)
    if reason is None:
        return BaselineChoice(name, found.metrics)
    if named is not None:
        msg = f"baseline {name} not comparable: {reason}"
        raise ConfigError(msg)
    return BaselineChoice(note=f"baseline {name} not comparable: {reason}")
