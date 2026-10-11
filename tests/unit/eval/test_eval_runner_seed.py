"""Tests of the `setup: seed_prior_run` hook of the eval runner (U11-66 step 5, U11-49; T11-33).

`run_golden` on the fixture warehouse of `_runner_env` with a recording seeder injected through
`EvalDeps.seed_prior_run` (herness.eval never imports tests/; the real seeding is
`tests.support.seed_ops.seed_prior_run`, exercised end to end by IT11-23).
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.ops_store import OpsStoreHandle
from tests.unit.eval._runner_env import (
    BUILD_ID,
    chat_q,
    lines,
    make_deps,
    make_root,
    runner_store,
    suite_file,
    test_redactor,
)

from herness.core.errors import QueryError
from herness.eval import _runner_question as rq
from herness.eval.golden import load_suite
from herness.eval.runner import EvalDeps, EvalOptions, run_golden

pytestmark = pytest.mark.unit
__all__ = ["runner_store", "test_redactor"]  # fixtures


class Seeder:
    """Records each call; raises `error` when set."""

    def __init__(self, error: Exception | None = None) -> None:
        self.calls: list[tuple[object, object, duckdb.DuckDBPyConnection, str]] = []
        self.error = error

    def __call__(
        self, memory: Any, truth: Any, *, warehouse: duckdb.DuckDBPyConnection, build_id: str
    ) -> object:
        self.calls.append((memory, truth, warehouse, build_id))
        if self.error is not None:
            raise self.error
        return "seeded"


MEMORY = object()  # the seeder only passes it through


def _run(root: Path, tmp: Path, deps: EvalDeps, questions: list[dict[str, Any]],
         repeat: int = 1) -> dict[tuple[str, int], dict[str, Any]]:  # fmt: skip
    suite = load_suite(suite_file(tmp / "suite.yaml", questions))
    run = run_golden(suite, "local", "fast", repeat=repeat, deps=deps, options=EvalOptions())
    return {(r["id"], r["repeat"]): r for r in lines(run.out_dir / "results.jsonl")}


def test_cv_t11_33_setup_seeds_once_then_question_graded(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-33 two `seed_prior_run` questions over two repeats: the injected seeder runs
    once per run with (memory, truth, warehouse, build_id) and every question is graded."""
    root = runner_store.data_root
    con = make_root(root)
    seeder = Seeder()
    deps = dataclasses.replace(make_deps(root, con), memory=MEMORY, seed_prior_run=seeder)  # type: ignore[arg-type]
    questions = [chat_q("G05", setup="seed_prior_run"), chat_q("G06", setup="seed_prior_run")]
    results = _run(root, tmp_path, deps, questions, repeat=2)
    assert {k: r["status"] for k, r in results.items()} == {
        (q, n): "passed" for q in ("G05", "G06") for n in (1, 2)
    }
    [(memory, truth, warehouse, build_id)] = seeder.calls
    assert memory is MEMORY
    assert truth is not None
    assert warehouse is con
    assert build_id == BUILD_ID


def test_cv_t11_33_setup_without_memory_or_seeder_is_suite_error(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-33 `EvalDeps.memory` or `EvalDeps.seed_prior_run` unset -> `suite_error` with
    a fixed detail; a question without setup is still graded."""
    root = runner_store.data_root
    con = make_root(root)
    no_memory = dataclasses.replace(make_deps(root, con), seed_prior_run=Seeder())
    results = _run(
        root, tmp_path, no_memory, [chat_q("G01"), chat_q("G05", setup="seed_prior_run")]
    )
    assert results[("G01", 1)]["status"] == "passed"
    assert results[("G05", 1)]["status"] == "suite_error"
    assert results[("G05", 1)]["error"] == f"SuiteError: {rq.SETUP_NO_DEPS}"
    no_seeder = dataclasses.replace(make_deps(root, con), memory=MEMORY)  # type: ignore[arg-type]
    (tmp_path / "b").mkdir()
    results = _run(root, tmp_path / "b", no_seeder, [chat_q("G05", setup="seed_prior_run")])
    assert results[("G05", 1)]["error"] == f"SuiteError: {rq.SETUP_NO_DEPS}"


def test_cv_t11_33_setup_on_a_real_build_is_suite_error(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-33 a real build has no truth to seed from -> `suite_error`; nothing seeded."""
    root = runner_store.data_root
    con = make_root(root, kind="real")
    seeder = Seeder()
    deps = dataclasses.replace(make_deps(root, con), memory=MEMORY, seed_prior_run=seeder)  # type: ignore[arg-type]
    results = _run(root, tmp_path, deps, [chat_q("G05", setup="seed_prior_run")])
    assert results[("G05", 1)]["status"] == "suite_error"
    assert results[("G05", 1)]["error"] == f"SuiteError: {rq.SETUP_NO_TRUTH}"
    assert seeder.calls == []


def test_cv_t11_33_seeding_error_is_question_error(
    runner_store: OpsStoreHandle, tmp_path: Path
) -> None:
    """CV-T11-33 a `HernessError` from the seeding makes the question `error` (U11-49
    propagates it; the runner never raises for a question)."""
    root = runner_store.data_root
    con = make_root(root)
    seeder = Seeder(QueryError("no incident table"))
    deps = dataclasses.replace(make_deps(root, con), memory=MEMORY, seed_prior_run=seeder)  # type: ignore[arg-type]
    results = _run(root, tmp_path, deps, [chat_q("G05", setup="seed_prior_run"), chat_q("G01")])
    assert results[("G05", 1)]["status"] == "error"
    assert results[("G05", 1)]["error"].startswith("QueryError: no incident table")
    assert results[("G01", 1)]["status"] == "passed"
