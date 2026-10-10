"""Fault tests of ``render_run`` (impl 09 FT09-03, FT09-06; T09-11).

FT09-03 kills a real child render process (``tests.support.render_kill``) after its first
``os.replace`` and re-renders in this process; FT09-06 deletes the run's warehouse file. Both
use the seeded ``report_env`` (``tiny_build`` copy, migrated ops store, fixture drafts).
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from typing import Final

import pytest
from tests.support.render_kill import KILLED_MARK, RESULT_PREFIX
from tests.support.report_env import BUILD_ID, FUND_RUN, REPO, ReportEnv

from herness._cli._exit_codes import exit_code_for
from herness.core.errors import ReportContractError
from herness.core.types import ReportManifest

pytestmark = pytest.mark.fault

TIMEOUT_S: Final = 300


def _child(env: ReportEnv, *extra: str) -> subprocess.CompletedProcess[str]:
    child_env = dict(os.environ, HERNESS_ENV="test", HERNESS_PATHS__DATA=str(env.data_root))
    child_env.pop("HERNESS_FAULTS", None)
    return subprocess.run(  # noqa: S603 - fixed module, our own interpreter
        [sys.executable, "-m", "tests.support.render_kill", str(env.data_root), FUND_RUN, *extra],
        cwd=REPO, env=child_env, capture_output=True, text=True, timeout=TIMEOUT_S, check=False,
    )  # fmt: skip


def test_ft09_03_killed_render_then_rerender(report_env: ReportEnv) -> None:
    """FT09-03 a render killed after its first replace leaves no manifest; the re-render
    removes the leftover tmp and writes complete files with the manifest last."""
    out = report_env.reports_root / FUND_RUN
    killed = _child(report_env, "kill")
    assert killed.returncode != 0, killed.stderr[-2000:]
    assert KILLED_MARK in killed.stderr
    assert (out / "report.html").is_file()
    assert not (out / "report.md").exists()
    assert not (out / "manifest.json").exists()
    leftovers = sorted(p.name for p in out.glob("*.tmp"))
    assert len(leftovers) == 1
    assert leftovers[0].startswith("report.md.")
    complete = _child(report_env)
    assert complete.returncode == 0, complete.stderr[-2000:]
    line = next(x for x in complete.stdout.splitlines() if x.startswith(RESULT_PREFIX))
    manifest = ReportManifest.model_validate_json(line.removeprefix(RESULT_PREFIX))
    assert not list(out.glob("*.tmp"))
    assert ReportManifest.model_validate_json((out / "manifest.json").read_bytes()) == manifest
    for name, digest in manifest.files.items():
        assert hashlib.sha256((out / name).read_bytes()).hexdigest() == digest
    assert report_env.render(FUND_RUN) == manifest  # same bytes from this process


def test_ft09_06_deleted_warehouse_is_build_retired(report_env: ReportEnv) -> None:
    """FT09-06 the run's warehouse file deleted → build-retired ReportContractError (exit 12)."""
    report_env.warehouse.unlink()
    with pytest.raises(ReportContractError) as info:
        report_env.render(FUND_RUN)
    exc = info.value
    assert exc.message == f"Build `{BUILD_ID}` used by this run was deleted by retention."
    assert exc.hint == "Re-run the review: `herness report funding`."
    assert exc.details["code"] == "build_retired"
    assert exit_code_for(exc) == 12  # R-46, the CLI mapping (U09-87)
    assert not (report_env.reports_root / FUND_RUN / "manifest.json").exists()
