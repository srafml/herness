"""Security tests of promotion (impl 02 ST02-17, TH02-17; T02-21: U02-103).

A build is never promoted on stale or missing DQ results: `--from-stage promote` (payload
`{"stages": ["promote"], "build_id": …}`) re-runs stage `dq` in the same job, so a
`duplicate_key` failure planted after the earlier gate passed blocks the promotion; an
empty `meta.dq_result` blocks too (fail closed). No promotion follows a yield in `score` or
inside the re-run `dq`. Runs on `lake_small` with fake spec 03 / 04 hooks.
"""

from __future__ import annotations

import dataclasses
import shutil
from pathlib import Path

import pytest
import structlog
from pydantic import BaseModel
from tests.support.build_harness import FakeJobContext
from tests.support.lake_small import install, load_config
from tests.support.ops_store import OpsStoreHandle
from tests.support.promote_builds import statuses

from herness.core.errors import HernessError, SchemaViolation
from herness.core.jobs.handlers import run_handler
from herness.core.resilience import ProcessState
from herness.core.types import JobOutcome
from herness.model import _build_stages as stages
from herness.model import _build_support as support
from herness.model import build
from herness.model.build import make_build_pipeline_handler
from herness.model.errors import DqGateFailed
from herness.model.sqlfiles import discover_sql_files
from herness.store import warehouse
from herness.store._warehouse_rw import open_for_build
from herness.store.layout import DataLayout

pytestmark = pytest.mark.integration

FULL = ["build", "enrich", "score", "dq", "promote"]
SQL_DIR = Path(build.__file__).parent / "sql"


class Report(BaseModel):
    """Stand-in for impl 03 `EnrichReport` / impl 04 `ScoringReport`."""

    name: str
    flags: list[str] = []


@dataclasses.dataclass(frozen=True)
class Env:
    layout: DataLayout


@pytest.fixture
def env(
    tmp_path: Path,
    ops_store: OpsStoreHandle,
    reset_process_state: ProcessState,
    monkeypatch: pytest.MonkeyPatch,
) -> Env:
    """`lake_small`, a loaded config and fake enrichment / scoring hooks."""
    install(ops_store.data_root)
    load_config(tmp_path / "cfgroot", ops_store.data_root)
    monkeypatch.setattr(stages, "_load_run_enrichment", lambda: lambda *_a, **_k: Report(name="e"))
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: lambda *_a, **_k: Report(name="s"))
    return Env(DataLayout.from_root(ops_store.data_root))


def _run(ctx: FakeJobContext) -> JobOutcome | HernessError:
    return run_handler(ctx, make_build_pipeline_handler(llm_factory=None))


def _done(ctx: FakeJobContext) -> JobOutcome:
    outcome = _run(ctx)
    assert isinstance(outcome, JobOutcome), outcome
    return outcome


def _prepared(env: Env) -> tuple[str, str]:
    """A promoted `CURRENT` build and a completed build whose own `dq` gate passed."""
    previous = str(_done(FakeJobContext({"stages": FULL})).result["build_id"])
    candidate = _done(FakeJobContext({"stages": ["build", "enrich", "score", "dq"]}))
    assert candidate.result["promoted"] is False
    assert warehouse.read_current(layout=env.layout) == previous
    return previous, str(candidate.result["build_id"])


def _from_stage_promote(build_id: str, **options: object) -> FakeJobContext:
    return FakeJobContext({"stages": ["promote"], "build_id": build_id}, **options)  # type: ignore[arg-type]


def test_st02_17_duplicate_key_blocks_from_stage_promote(env: Env) -> None:
    """ST02-17 a `duplicate_key` failure planted after the build's own gate passed: `--from-
    stage promote` re-runs DQ, `DqGateFailed` names it, the build is `failed`, `CURRENT` is
    unchanged and nothing is promoted."""
    previous, candidate = _prepared(env)
    view = support.DbSettings(threads=1, memory_limit="512MiB")
    with open_for_build(candidate, create=False, cfg=view, layout=env.layout) as con:
        con.execute("INSERT INTO core.incident SELECT * FROM core.incident LIMIT 1")
    with structlog.testing.capture_logs() as logs:
        error = _run(_from_stage_promote(candidate))
    assert isinstance(error, DqGateFailed), error
    assert "duplicate_key:core.incident" in error.failed_checks
    assert warehouse.read_current(layout=env.layout) == previous
    assert statuses(env.layout) == {previous: "promoted", candidate: "failed"}
    [evaluated] = [e for e in logs if e["event"] == "model.dq.evaluated"]
    assert evaluated["build_id"] == candidate
    assert not [e for e in logs if e["event"] == "model.build.promoted"]
    [failed] = [e for e in logs if e["event"] == "model.build.failed"]
    assert (failed["stage"], failed["error_class"]) == ("promote", "DqGateFailed")


def test_st02_17_empty_dq_result_blocks(
    env: Env, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """ST02-17 a DQ re-run that leaves `meta.dq_result` empty fails closed (SchemaViolation
    `no DQ results`): the build is `failed`, `CURRENT` unchanged."""
    previous, candidate = _prepared(env)
    copy = tmp_path / "sql"
    shutil.copytree(SQL_DIR, copy)
    (copy / "900_dq_checks.sql").write_text("DELETE FROM meta.dq_result;\n", encoding="utf-8")
    monkeypatch.setattr(build, "discover_sql_files", lambda: discover_sql_files(sql_dir=copy))
    error = _run(_from_stage_promote(candidate))
    assert type(error) is SchemaViolation, error
    assert str(error) == "no DQ results for build"
    assert warehouse.read_current(layout=env.layout) == previous
    assert statuses(env.layout) == {previous: "promoted", candidate: "failed"}


def test_st02_17_from_stage_promote_reruns_passing_gate(env: Env) -> None:
    """ST02-17 an unchanged build: `--from-stage promote` re-runs DQ in its own job (the
    result carries `dq`), then promotes; the previous build is retired."""
    previous, candidate = _prepared(env)
    with structlog.testing.capture_logs() as logs:
        outcome = _done(_from_stage_promote(candidate))
    assert outcome.result["promoted"] is True
    assert list(outcome.result["durations_ms"]) == ["promote"]  # type: ignore[arg-type]
    dq = outcome.result["dq"]
    assert isinstance(dq, dict)
    assert dq["failed_errors"] == []
    assert [e["build_id"] for e in logs if e["event"] == "model.dq.evaluated"] == [candidate]
    assert warehouse.read_current(layout=env.layout) == candidate
    assert statuses(env.layout) == {candidate: "promoted", previous: "retired"}


def test_st02_17_no_promotion_after_score_yield(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST02-17 (U02-104 spec note) a scoring report flagged `yielded` makes the job yield
    before `dq` / `promote`: nothing is promoted, `CURRENT` stays absent."""
    yielded = Report(name="s", flags=["yielded"])
    monkeypatch.setattr(stages, "_load_run_scoring", lambda: lambda *_a, **_k: yielded)
    ctx = FakeJobContext({"stages": FULL})
    outcome = _done(ctx)
    assert outcome.status == "yield"
    assert ctx.load_state()["stages_done"] == ["build", "enrich"]
    assert warehouse.read_current(layout=env.layout) is None
    assert [b.status for b in warehouse.list_builds(layout=env.layout)] == ["building"]


def test_st02_17_no_promotion_after_yield_in_dq_rerun(env: Env) -> None:
    """ST02-17 a yield inside the DQ re-run of `--from-stage promote` returns `yield`: the
    build stays `building` and `CURRENT` unchanged."""
    previous, candidate = _prepared(env)
    ctx = _from_stage_promote(candidate, yield_after=1)  # stage check, then the first 9xx file
    outcome = _done(ctx)
    assert outcome.status == "yield"
    assert "promote" not in ctx.load_state()["stages_done"]  # type: ignore[operator]
    assert warehouse.read_current(layout=env.layout) == previous
    assert statuses(env.layout) == {previous: "promoted", candidate: "building"}
