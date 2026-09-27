"""Fault tests for herness.metrics.scoring (impl 04 FT04-01 metrics part, FT04-03; T04-13).

FT04-01: a real child process (`tests.support.scoring_kill`) runs `run_scoring` on a tiny build
file and is terminated by the OS (`Popen.kill`, no fault point, R-40) while the `check` step's
transaction is open, i.e. after `metrics.scoring.step_started` for `check`. The restarted job
skips `metrics` through the file-backed checkpoint, rewrites `check`, and every table equals a
clean run. The funding half of FT04-01 is T04-21's (the funding step is not built yet).
FT04-03: a metric template raising a DuckDB error rolls the whole metrics step back.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import duckdb
import pytest
from tests.support.build_harness import FakeJobContext
from tests.support.metrics_scoring import (
    CFG_HASH,
    patches,
    save_as_file,
    small_catalog,
    tiny_with_facts,
)
from tests.support.metrics_tiny import BUILD_ID
from tests.support.scoring_kill import BLOCKED_LINE, RESULT_PREFIX

from herness.core.errors import SchemaViolation
from herness.metrics import _scoring_checks, scoring
from herness.metrics.catalog import MetricCatalog
from herness.metrics.scoring import run_scoring

pytestmark = pytest.mark.fault

REPO = Path(__file__).resolve().parents[3]
CHILD_TIMEOUT_S = 300
_SNAPSHOT_SQL = (
    ("metric_value", "SELECT * FROM metrics.metric_value"),
    ("dq_result", "SELECT * FROM meta.dq_result"),
    (
        "evidence",
        "SELECT query_id, sql, params, result_hash, row_count, producer FROM meta.evidence",
    ),
    ("build", "SELECT build_id, status FROM meta.build"),
)


def _apply(mp: pytest.MonkeyPatch, catalog: MetricCatalog) -> None:
    for obj, name, value in patches(catalog):
        mp.setattr(obj, name, value)


def _snapshot(path: Path) -> dict[str, list[str]]:
    con = duckdb.connect(str(path), read_only=True)
    try:
        return {
            name: sorted(repr(row) for row in con.execute(sql).fetchall())
            for name, sql in _SNAPSHOT_SQL
        }
    finally:
        con.close()


def _argv(db: Path, state: Path, *extra: str) -> list[str]:
    return [sys.executable, "-m", "tests.support.scoring_kill", str(db), str(state), *extra]


def _env() -> dict[str, str]:
    return {**os.environ, "PYTHONUTF8": "1"}


def _result(stdout: str) -> dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.startswith(RESULT_PREFIX)]
    assert len(lines) == 1, stdout[-2000:]
    report: dict[str, Any] = json.loads(lines[0].removeprefix(RESULT_PREFIX))
    return report


@pytest.fixture
def build_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
    """Two copies of the tiny build file with facts: (for the killed job, for a clean run)."""
    _apply(monkeypatch, small_catalog())
    killed, clean = tmp_path / "killed.duckdb", tmp_path / "clean.duckdb"
    con = tiny_with_facts()
    try:
        save_as_file(con, killed)
        save_as_file(con, clean)
    finally:
        con.close()
    return killed, clean


def test_ft04_01_kill_in_check_then_restart(build_files: tuple[Path, Path], tmp_path: Path) -> None:
    """FT04-01 kill the job's child process after `step_started` for `check`; the restart skips
    `metrics` via the checkpoint, rewrites `check`, and all tables equal a clean run."""
    killed, clean = build_files
    state = tmp_path / "state.json"
    child = subprocess.Popen(  # noqa: S603 - fixed argv, no shell
        _argv(killed, state, "block"),
        cwd=REPO,
        env=_env(),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    seen: list[str] = []
    try:
        assert child.stdout is not None
        for line in child.stdout:
            seen.append(line)
            if line.strip() == BLOCKED_LINE:
                break
        else:
            pytest.fail("child ended before blocking in check: " + "".join(seen)[-2000:])
        child.kill()  # TerminateProcess / SIGKILL: no handler, no cleanup in the child
    finally:
        child.communicate(timeout=CHILD_TIMEOUT_S)
    assert child.returncode != 0
    started = [line for line in seen if "metrics.scoring.step_started" in line]
    assert ["step=check" in line for line in started] == [False, True]  # metrics, then check
    saved = json.loads(state.read_text(encoding="utf-8"))
    assert saved["scoring"] == {
        "build_id": BUILD_ID,
        "config_hash": CFG_HASH,
        "steps_done": ["metrics"],
    }
    check_rows = _snapshot(killed)["dq_result"]
    assert not any("score_metric_ratio_range" in row for row in check_rows)  # rolled back

    restart = subprocess.run(  # noqa: S603 - fixed argv, no shell
        _argv(killed, state),
        cwd=REPO,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    assert restart.returncode == 0, restart.stdout[-2000:] + restart.stderr[-2000:]
    report = _result(restart.stdout)
    assert report["steps_done"] == ["validate", "metrics", "check"]
    assert set(report["duration_ms"]) == {"check"}
    assert "metrics.scoring.step_skipped" in restart.stdout

    clean_run = subprocess.run(  # noqa: S603 - fixed argv, no shell
        _argv(clean, tmp_path / "clean_state.json"),
        cwd=REPO,
        env=_env(),
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=CHILD_TIMEOUT_S,
        check=False,
    )
    assert clean_run.returncode == 0, clean_run.stdout[-2000:] + clean_run.stderr[-2000:]
    assert _snapshot(killed) == _snapshot(clean)


def _broken_catalog() -> MetricCatalog:
    """The small catalog plus `zz_broken_metric` (sorted last, so rows of the other metrics are
    appended first): `mttr_hours` whose value raises at run time."""
    base = small_catalog()
    mttr = next(m for m in base.config.metrics if m.name == "mttr_hours")
    broken = mttr.model_copy(
        update={
            "name": "zz_broken_metric",
            "enabled": True,
            "sql": mttr.sql.replace("avg(", "CAST('planted failure' AS INTEGER) + avg(", 1),
        }
    )
    assert broken.sql != mttr.sql
    metrics = [*base.config.metrics, broken]
    return MetricCatalog(base.config.model_copy(update={"metrics": metrics}))


@pytest.mark.parametrize("earlier_run", [False, True])
def test_ft04_03_metric_template_error_rolls_back(
    monkeypatch: pytest.MonkeyPatch, earlier_run: bool
) -> None:
    """FT04-03 a metric template raising a DuckDB error: SchemaViolation, the metrics step is
    rolled back (no partial `metric_value`, no evidence of it, an earlier table unchanged), the
    step is not checkpointed and the build stays `building` (not promoted)."""
    _apply(monkeypatch, small_catalog())
    con = tiny_with_facts()
    try:
        if earlier_run:
            run_scoring(BUILD_ID, steps=["metrics"], con=con)
        before = {
            name: sorted(repr(r) for r in con.execute(sql).fetchall())
            for name, sql in _SNAPSHOT_SQL
            if earlier_run or name != "metric_value"
        }
        monkeypatch.setattr(scoring, "catalog_from_config", lambda _cfg=None: _broken_catalog())
        ctx = FakeJobContext()
        with pytest.raises(
            SchemaViolation, match=r"^metric zz_broken_metric grain \w+ period week failed: "
        ):
            run_scoring(BUILD_ID, con=con, ctx=ctx)
        after = {
            name: sorted(repr(r) for r in con.execute(sql).fetchall())
            for name, sql in _SNAPSHOT_SQL
            if earlier_run or name != "metric_value"
        }
        assert after == before
        tables = _scoring_checks.existing_tables(con)
        assert ("metrics.metric_value" in tables) is earlier_run
        assert ctx.saved_states == []
    finally:
        con.close()
