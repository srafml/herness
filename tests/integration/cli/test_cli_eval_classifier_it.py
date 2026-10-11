"""CLI-level acceptance of `herness eval --suite classifier` (T09-24, U09-98; R-43; T11-35
carry-over): the real Typer app in-process as an admin (``cli_env``) on the T11-35 fixture
model directory (``tests/unit/eval/_classifier_env``). The CLI config carries the fixture's
question set; the composition root's factory is replaced by the fixture deps; the payload,
the ops-store job (GPU class ``decider``), impl 08's ``run_inline`` (GPU step stubbed),
``handle_eval`` and the exit code are real.
"""

from __future__ import annotations

import json
import sqlite3

import pytest
from tests.support.cli_env import CliEnv
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.enrich import _evaluate_fixtures as fx
from tests.unit.eval._classifier_env import ClassifierEnv, classifier_env, set_eval_metric
from tests.unit.eval._runner_env import make_deps

from herness import cli
from herness._cli import _eval_deps
from herness.core.jobs import inline as jobs_inline

pytestmark = pytest.mark.integration
__all__ = ["classifier_env"]  # fixture


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


@pytest.fixture
def env(
    cli_env: CliEnv, classifier_env: ClassifierEnv, fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch,
) -> ClassifierEnv:  # fmt: skip
    """`root` is an admin; the CLI config holds the fixture question set; the redaction key
    the run's config hash needs is in the (fake) keyring."""
    fake_keyring.store[("herness", "redact.hmac_key")] = "ab" * 32
    cfg_dir = cli_env.write_config(admins=["root"])
    (cfg_dir / "decisions.yaml").write_text("version: 1\n" + fx.DECISIONS_YAML, encoding="utf-8")
    cli_env.user = "root"
    data_root, con = classifier_env.data_root, classifier_env.con
    monkeypatch.setattr(_eval_deps, "make_deps", lambda c: make_deps(data_root, con, job=c))
    monkeypatch.setattr(_eval_deps._Root, "path", None)
    monkeypatch.setattr(jobs_inline, "_gpu_controller", lambda *args: None)
    return classifier_env


def test_cv_t09_24_eval_cli_suite_classifier_decider_job(
    env: ClassifierEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-T09-24 `herness eval --suite classifier --inline`: one `eval` job with GPU class
    `decider` (R-43), `classifier.json` written, exit 0; a stored metric off by 0.01 → 14."""
    args = ["--json", "eval", "--suite", "classifier", "--inline"]
    capsys.readouterr()
    assert _main(args) == 0
    data = json.loads(capsys.readouterr().out)["data"]
    assert data["passed"] is True
    assert (env.out_dir(data["run_id"]) / "classifier.json").is_file()
    set_eval_metric(env, "root_cause", "accuracy", 0.01)
    assert _main(args) == 14
    assert json.loads(capsys.readouterr().out)["data"]["passed"] is False
    with sqlite3.connect(env.data_root / "ops.sqlite") as con:
        rows = list(con.execute("SELECT kind, gpu_class, status FROM job ORDER BY created_at"))
    assert rows == [("eval", "decider", "done"), ("eval", "decider", "done")]
