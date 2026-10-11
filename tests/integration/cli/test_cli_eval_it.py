"""CLI-level acceptance of `herness eval` through ``herness.cli.main`` (T09-24, U09-98; impl 11
U11-68, U11-69; T11-30, T11-31, T11-38 carry-overs).

The real Typer app runs in-process as an admin (``cli_env``) on the test-local eval setup of
``tests/integration/eval/_eval_it_env`` (``tiny_build`` warehouse, the real swarm driver with
scripted agents, a scripted chat service; suites written per test, T11-32's golden content is
not on this base). The composition root's factory ``_eval_deps.make_deps`` is replaced by that
environment's ``deps``; everything else is real: the payload, the ops-store job, impl 08's
``run_inline`` (``--inline``, R-45; the GPU step stubbed), ``handle_eval`` and the exit code.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import jsonschema
import pytest
from tests.integration.eval._eval_it_env import EvalEnv, eval_env, question
from tests.integration.harness import _swarm_run_env as e
from tests.support.cli_env import CliEnv
from tests.unit.harness._blackboard_env import test_redactor

from herness import cli
from herness._cli import _eval_deps
from herness.core.jobs import inline as jobs_inline

pytestmark = pytest.mark.integration

drv = e.drv  # fixtures
__all__ = ["drv", "eval_env", "test_redactor"]

CLI1_SCHEMA = json.loads(
    (
        Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "cli" / "cli1.schema.json"
    ).read_text(encoding="utf-8")
)
JUDGE_SCRIPT = "- match: {model_role: eval_judge}\n  turns:\n    - final: {text: ok}\n"


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


class Cli:
    """The admin CLI over the eval environment."""

    def __init__(self, env: EvalEnv, capsys: pytest.CaptureFixture[str]) -> None:
        self.env, self.capsys = env, capsys

    def run(self, suite: Path | str, *extra: str) -> tuple[int, dict[str, Any]]:
        self.capsys.readouterr()
        args = ["--json", "eval", "--inline", "--suite", str(suite), "--profile", "synth",
                "--depth", "fast", "--no-judge", *extra]  # fmt: skip
        code = _main(args)
        out = self.capsys.readouterr().out
        return code, json.loads(out)

    def mock_dir(self) -> Path:
        mock = self.env.tmp / "mock"
        mock.mkdir(exist_ok=True)
        (mock / "judge.yaml").write_text(JUDGE_SCRIPT, encoding="utf-8")
        return mock

    def jobs(self) -> list[tuple[str, str, str]]:
        with sqlite3.connect(self.env.data_root / "ops.sqlite") as con:
            rows = con.execute("SELECT kind, gpu_class, status FROM job ORDER BY created_at")
            return [tuple(row) for row in rows]


@pytest.fixture
def admin_cli(
    cli_env: CliEnv, eval_env: EvalEnv, monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> Cli:  # fmt: skip
    """`root` is an admin; the eval factory is the environment's; no GPU step in tests."""
    cli_env.write_config(admins=["root"])
    cli_env.user = "root"
    monkeypatch.setattr(_eval_deps, "make_deps", eval_env.deps)
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(jobs_inline, "_gpu_controller", lambda *args: None)
    return Cli(eval_env, capsys)


def test_cv_t09_24_eval_cli_single_question_exit_0(admin_cli: Cli) -> None:
    """CV-T09-24 `herness eval --inline --ids G01` on tiny_build: one `eval` job (reasoning)
    done, `results.jsonl` one passed line, exit 0 (T11-30 CLI acceptance)."""
    env = admin_cli.env
    suite = env.suite([question("G01"), question("G02")])
    code, payload = admin_cli.run(suite, "--ids", "G01")
    assert code == 0, payload
    data = payload["data"]
    assert (data["status"], data["passed"]) == ("done", True)
    lines = (env.out_dir(data["run_id"]) / "results.jsonl").read_text("utf-8").splitlines()
    assert [json.loads(line)["id"] for line in lines] == ["G01"]
    assert admin_cli.jobs() == [("eval", "reasoning", "done")]


def test_cv_t09_24_eval_cli_repeated_ids_and_unknown_id(admin_cli: Cli) -> None:
    """CV-T09-24 (T11-37) `--ids G01 --ids G02,G01` runs G01 and G02 once each (exit 0);
    an id the suite lacks is ConfigError naming it (exit 3) and enqueues nothing."""
    env = admin_cli.env
    suite = env.suite([question("G01"), question("G02"), question("G03")])
    code, payload = admin_cli.run(suite, "--ids", "G01", "--ids", "G02,G01")
    assert code == 0, payload
    lines = (env.out_dir(payload["data"]["run_id"]) / "results.jsonl").read_text("utf-8")
    assert [json.loads(line)["id"] for line in lines.splitlines()] == ["G01", "G02"]
    code, payload = admin_cli.run(suite, "--ids", "G01,G42")
    assert code == 3
    assert payload["error"]["message"] == "unknown question ids: G42"
    assert admin_cli.jobs() == [("eval", "reasoning", "done")]


def test_cv_t09_24_eval_cli_threshold_fail_exit_14(admin_cli: Cli) -> None:
    """CV-T09-24 a `suite_error` question (bad reference SQL) fails an eval gate:
    `passed = false`, exit 14 (R-46)."""
    env = admin_cli.env
    bad_sql = question("G04", expected={"numeric": {
        "reference_sql": "SELECT value FROM core.no_such_table", "unit": "count"}})  # fmt: skip
    code, payload = admin_cli.run(env.suite([question("G01"), bad_sql]), "--ids", "G01,G04")
    assert code == 14, payload
    assert payload["data"]["passed"] is False
    assert payload["ok"] is False
    assert admin_cli.jobs() == [("eval", "reasoning", "done")]


def test_it09_15_eval_gate_failed_exit_14(admin_cli: Cli) -> None:
    """IT09-15 ``herness --json eval`` whose gate fails (a ``suite_error`` question): exit 14
    (R-46, U09-87), ``ok`` false with the job data; the envelope is valid ``cli/1``
    (``tests/fixtures/cli/cli1.schema.json``) and ``error`` stays null."""
    env = admin_cli.env
    bad_sql = question("G05", expected={"numeric": {
        "reference_sql": "SELECT n FROM core.missing_table", "unit": "count"}})  # fmt: skip
    code, payload = admin_cli.run(env.suite([bad_sql]), "--ids", "G05")
    assert code == 14, payload
    jsonschema.Draft202012Validator(CLI1_SCHEMA).validate(payload)
    assert (payload["ok"], payload["error"]) == (False, None)
    assert payload["data"]["passed"] is False
    assert payload["data"]["status"] == "done"


def test_cv_t09_24_eval_cli_bad_suite_path_exit_3(admin_cli: Cli) -> None:
    """CV-T09-24 a missing suite file is ConfigError in the handler: exit 3, job failed."""
    code, payload = admin_cli.run(admin_cli.env.tmp / "missing.yaml", "--ids", "G01")
    assert code == 3
    assert payload["error"]["type"] == "ConfigError"
    assert admin_cli.jobs() == [("eval", "reasoning", "failed")]


def test_cv_t09_24_eval_cli_set_baseline_mock_naming(admin_cli: Cli) -> None:
    """CV-T09-24 `--mock-llm` runs (GPU class none) may only save a `-mock` baseline (T11-31,
    TH11-10): another name exits 3 before any question runs; `tiny-mock` is saved."""
    env = admin_cli.env
    suite, mock = env.suite([question("G01")]), admin_cli.mock_dir()
    refused, payload = admin_cli.run(suite, "--ids", "G01", "--mock-llm", str(mock),
                                     "--set-baseline", "synthetic-42-small")  # fmt: skip
    assert refused == 3, payload
    assert not (env.tmp / "tests" / "eval" / "baselines" / "synthetic-42-small.json").exists()
    code, payload = admin_cli.run(suite, "--ids", "G01", "--mock-llm", str(mock),
                                  "--set-baseline", "tiny-mock")  # fmt: skip
    assert code == 0, payload
    assert (env.tmp / "tests" / "eval" / "baselines" / "tiny-mock.json").is_file()
    assert admin_cli.jobs() == [("eval", "none", "failed"), ("eval", "none", "done")]


def test_cv_t09_24_eval_cli_compare_and_compare_runs(admin_cli: Cli) -> None:
    """CV-T09-24 `--compare local` runs twice on one build and prints both run ids (exit 0);
    `--compare-runs A,B` then compares the stored runs in-process and enqueues nothing."""
    env = admin_cli.env
    suite, mock = env.suite([question("G01")]), admin_cli.mock_dir()
    code, payload = admin_cli.run(suite, "--ids", "G01", "--mock-llm", str(mock),
                                  "--compare", "local")  # fmt: skip
    assert code == 0, payload
    first, second = payload["data"]["run_id"], payload["data"]["compare_run_id"]
    table = json.loads((env.out_dir(second) / "comparison.json").read_text("utf-8"))
    assert table["run_ids"] == [first, second]
    (env.out_dir(second) / "comparison.json").unlink()
    admin_cli.capsys.readouterr()
    assert _main(["--json", "eval", "--compare-runs", f"{first},{second}"]) == 0
    data = json.loads(admin_cli.capsys.readouterr().out)["data"]
    assert (data["run_id"], data["harness_defects"]) == (second, [])
    assert (env.out_dir(second) / "comparison.json").is_file()
    assert admin_cli.jobs() == [("eval", "none", "done")]  # the compare made no job
