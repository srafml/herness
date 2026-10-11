"""Tests for herness._cli.cmd_admin and its siblings _admin_config and _eval_cmd (T09-24, U09-98).

Commands run through ``herness.cli.main`` as the OS user ``root`` (an admin) in an elevated
shell (``cli_env``). The owner functions (impl 10 ``herness.admin.commands_*``, impl 03
``laya_admin``, impl 11 ``herness.eval``) are replaced on their modules, which the command
bodies import lazily; job submission is spied on the command modules.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
from tests.support.cli_env import CliEnv, preserved_app
from tests.support.fake_keyring import MemoryKeyring
from tests.unit.eval._golden_fixtures import write_suite
from typer.testing import CliRunner

import herness.admin
import herness.core.jobs.handlers
import herness.eval.report
from herness import cli
from herness._cli import _eval_cmd, _eval_deps, _job_cmds, _worker_boot, cmd_admin, identity
from herness._cli.output import CommandResult
from herness._cli.wait import FollowOutcome
from herness.admin import commands_config, commands_data, commands_deploy, commands_secrets
from herness.core.config_view import ConfigIssue
from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.jobs import JobRow
from herness.enrich import laya_admin
from herness.eval import runner
from herness.reports import rules

pytestmark = pytest.mark.unit

JOB = "job_01JA0000000000000000000001"
RUN = "run_01JA0000000000000000000001"
RUN2 = "run_01JA0000000000000000000002"
HASH_RE = r"^[0-9a-f]{32}$"
NEW_COMMANDS = (
    "config validate", "config show", "config hash", "secrets init", "secrets set",
    "secrets status", "secrets rekey", "deploy render", "deploy pull", "deploy up",
    "deploy down", "deploy rollback", "deploy prune", "deploy install", "eval", "distill",
    "laya status", "laya accept", "laya rollback", "privacy delete", "maintenance backup",
    "maintenance purge",
)  # fmt: skip


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


def _ok(data: dict[str, object] | None = None, *, code: int = 0,
        warnings: list[str] | None = None) -> CommandResult:  # fmt: skip
    return CommandResult(ok=code == 0, data=data or {}, warnings=warnings or [], exit_code=code)


class Recorder:
    """Records the owner calls ``(name, args, kwargs)`` and answers with ``result``."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def fake(self, name: str, result: object = None) -> Any:
        def call(*args: Any, **kwargs: Any) -> object:
            self.calls.append((name, args, kwargs))
            return _ok({"name": name}) if result is None else result

        return call

    def names(self) -> list[str]:
        return [name for name, _, _ in self.calls]


@pytest.fixture
def admin(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    """``root`` is an admin in an elevated shell; ``alice`` is a viewer."""
    cli_env.write_config(admins=["root"])
    cli_env.user = "root"
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    monkeypatch.setattr(runner._Binding, "factory", None)  # `eval` binds it
    monkeypatch.setattr(_eval_deps._Root, "path", None)  # ... and sets the checkout root
    return Recorder()


# --- config (UT09-90) ----------------------------------------------------------------------------

VALIDATED = {"profile": "local", "config_hash": "cfg_0123456789abcdef", "issues": []}


def _validate(admin: Recorder, monkeypatch: pytest.MonkeyPatch, issues: list[ConfigIssue],
              result: CommandResult | None = None) -> None:  # fmt: skip
    monkeypatch.setattr(commands_config, "cmd_config_validate",
                        admin.fake("validate", result or _ok(dict(VALIDATED))))  # fmt: skip
    monkeypatch.setattr(cli, "run_startup_validation", admin.fake("startup", issues))


def test_ut09_90_config_validate_appends_startup_issues(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `config validate` calls cmd_config_validate(config_dir, profile, offline,
    strict), then appends the start-up hook's issues (R-71) and exits 1 on an error issue."""
    _validate(admin, monkeypatch, [ConfigIssue("error", "metrics", "bad catalog", "metrics.yaml")])
    assert _main(["--json", "config", "validate"]) == 1
    (_, _, kwargs), (_, sargs, skw) = admin.calls
    assert kwargs == {"config_dir": Path("config"), "offline": False, "strict": False,
                      "profile": None}  # fmt: skip
    assert skw["raise_on_error"] is False
    assert skw["config_dir"] == Path("config")
    assert sargs[0].profile == "local"
    payload = _json(capsys)
    assert payload["ok"] is False
    assert payload["data"]["issues"] == ["error metrics metrics.yaml: bad catalog"]


def test_ut09_90_config_validate_strict_warning_exits_1(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `config validate --strict` with a warning → 1; without --strict → 0; human
    output prints the issue lines, the profile and the config hash."""
    _validate(admin, monkeypatch, [ConfigIssue("warn", "metrics", "unused", None)])
    assert _main(["config", "validate", "--strict", "--profile", "synth"]) == 1
    assert admin.calls[0][2]["strict"] is True
    assert admin.calls[0][2]["profile"] == "synth"
    assert admin.calls[1][1][0].profile == "synth"  # the validated config is the named one
    out = capsys.readouterr().out
    assert out.splitlines() == ["warn metrics -: unused", "profile: local",
                                "config_hash: cfg_0123456789abcdef"]  # fmt: skip
    assert _main(["config", "validate"]) == 0


def test_ut09_90_config_validate_offline_or_unloadable_skips_startup_hook(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-90 `--offline`, or a config that failed to load, runs no owner validator; the
    owner's exit code is kept (1 for a strict warning, R-46)."""
    _validate(admin, monkeypatch, [ConfigIssue("error", "x", "never", None)],
              _ok(dict(VALIDATED), code=1))  # fmt: skip
    assert _main(["config", "validate", "--offline", "--strict"]) == 1
    failed = dict(VALIDATED, config_hash=None, issues=["error config -: broken"])
    _validate(admin, monkeypatch, [], _ok(failed, code=1))
    assert _main(["config", "validate"]) == 1
    assert admin.names() == ["validate", "validate"]


def test_ut09_90_config_show_and_hash(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `config show` prints YAML and keeps `secret:<name>`; `config hash` passes the
    profile; both call their impl 10 function with the config dir."""
    shown = {"sources": {"jira": {"token": "secret:jira.token"}}}
    monkeypatch.setattr(commands_config, "cmd_config_show", admin.fake("show", _ok(shown)))
    monkeypatch.setattr(commands_config, "cmd_config_hash",
                        admin.fake("hash", _ok({"config_hash": "cfg_1"})))  # fmt: skip
    assert _main(["config", "show"]) == 0
    assert capsys.readouterr().out.splitlines() == ["sources:", "  jira:",
                                                    "    token: secret:jira.token"]  # fmt: skip
    assert _main(["--json", "config", "show", "--profile", "hybrid"]) == 0
    assert _json(capsys)["data"]["sources"] == shown["sources"]
    assert _main(["--json", "config", "hash", "--profile", "synth"]) == 0
    assert _json(capsys)["data"]["config_hash"] == "cfg_1"
    assert [kw for _, _, kw in admin.calls] == [
        {"config_dir": Path("config"), "profile": None},
        {"config_dir": Path("config"), "profile": "hybrid"},
        {"config_dir": Path("config"), "profile": "synth"},
    ]


# --- secrets (UT09-90, TH09-20) -------------------------------------------------------------------


def test_ut09_90_secrets_set_hidden_prompt_and_actor(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `secrets set NAME`: cmd_secrets_set(name, actor=user_ref, prompt) where prompt
    is typer.prompt(hide_input=True); `--json` is refused before any prompt (exit 2)."""
    prompts: list[tuple[str, dict[str, Any]]] = []
    monkeypatch.setattr(typer, "prompt", lambda text, **kw: prompts.append((text, kw)) or "v")

    def fake_set(name: str, *, actor: str, prompt: Any) -> CommandResult:
        admin.calls.append(("set", (name,), {"actor": actor, "value": prompt("Value")}))
        return _ok({"name": name, "stored": True})

    monkeypatch.setattr(commands_secrets, "cmd_secrets_set", fake_set)
    assert _main(["secrets", "set", "snow.token"]) == 0
    ((_, args, kwargs),) = admin.calls
    assert args == ("snow.token",)
    assert kwargs["value"] == "v"
    assert re.fullmatch(HASH_RE, kwargs["actor"])
    assert prompts == [("Value", {"hide_input": True})]
    capsys.readouterr()
    assert _main(["--json", "secrets", "set", "snow.token"]) == 2
    assert _json(capsys)["error"]["type"] == "UserInputError"
    assert len(admin.calls) == 1


def test_ut09_90_secrets_set_never_takes_the_value_as_argument(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-90 acceptance (CliRunner): `secrets set NAME VALUE` and `--value` are usage errors;
    the owner body is never called, so no value from the command line can be stored."""
    monkeypatch.setattr(commands_secrets, "cmd_secrets_set", admin.fake("set"))
    with preserved_app():
        cli._register_groups(cli.app)
        runner_ = CliRunner()
        for args in (["secrets", "set", "snow.token", "s3cr3t-value"],
                     ["secrets", "set", "snow.token", "--value", "s3cr3t-value"]):  # fmt: skip
            result = runner_.invoke(cli.app, args, obj=cli.GlobalOptions())
            assert result.exit_code == 2
            assert "s3cr3t-value" not in result.stdout
    assert admin.calls == []


def test_ut09_90_secrets_init_rekey_status(
    admin: Recorder, cli_env: CliEnv, fake_keyring: MemoryKeyring,
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-90 `secrets init` and `rekey` get actor, prompt and a stderr `show`; `status` the
    loaded config; init audits as `system` before the user-ref key exists (fresh install)."""

    def fake(name: str) -> Any:
        def call(**kw: Any) -> CommandResult:
            admin.calls.append((name, (), kw))
            if "show" in kw:
                kw["show"]("generated-key-value")
            return _ok({"secrets": []})

        return call

    monkeypatch.setattr(commands_secrets, "cmd_secrets_init", fake("init"))
    monkeypatch.setattr(commands_secrets, "cmd_secrets_rekey", fake("rekey"))
    monkeypatch.setattr(commands_secrets, "cmd_secrets_status", fake("status"))
    for command in ("init", "rekey", "status"):
        assert _main(["secrets", command]) == 0
    out, err = capsys.readouterr()
    assert "generated-key-value" not in out
    assert err.count("generated-key-value") == 2
    init, rekey, status = (kw for _, _, kw in admin.calls)
    assert init["actor"] == rekey["actor"]
    assert re.fullmatch(HASH_RE, init["actor"])
    assert callable(init["prompt"])
    assert callable(rekey["prompt"])
    assert status["cfg"].profile == "local"
    for command in ("init", "rekey"):
        assert _main(["--json", "secrets", command]) == 2
    assert len(admin.calls) == 3
    del fake_keyring.store[("herness", "ui_user_ref_key")]
    rules.load_user_ref_key.cache_clear()  # read once per process
    assert _main(["secrets", "init"]) == 0
    assert admin.calls[-1][2]["actor"] == "system"


def test_ut09_90_secrets_need_elevation_and_admin(
    admin: Recorder, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-90 checks before the call (U09-89): no elevated shell → 11; a viewer → 11."""
    monkeypatch.setattr(commands_secrets, "cmd_secrets_set", admin.fake("set"))
    monkeypatch.setattr(identity, "_is_elevated", lambda: False)
    assert _main(["secrets", "set", "snow.token"]) == 11
    monkeypatch.setattr(identity, "_is_elevated", lambda: True)
    cli_env.user = "alice"
    assert _main(["secrets", "set", "snow.token"]) == 11
    assert admin.calls == []


# --- deploy --------------------------------------------------------------------------------------


@pytest.fixture
def deploy(admin: Recorder, monkeypatch: pytest.MonkeyPatch) -> Recorder:
    for op in ("render", "pull", "up", "down", "rollback", "prune", "install"):
        monkeypatch.setattr(commands_deploy, f"cmd_deploy_{op}", admin.fake(op))
    return admin


def test_ut09_90_deploy_commands_call_owner_with_cfg_and_actor(
    deploy: Recorder, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 each `deploy` command calls its cmd_deploy_* with the parsed arguments, the
    loaded config and the actor's user_ref; `deploy down` without CLASS stops every class."""
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for args in (
        ["render"],
        ["pull", "--allow-download"],
        ["up", "decider"],
        ["down", "large"],
        ["rollback", "reasoning"],
        ["prune"],
        ["install", str(bundle)],
        ["down"],
    ):
        assert _main(["deploy", *args]) == 0, args
    calls = [(name, args) for name, args, _ in deploy.calls]
    assert calls == [
        ("render", ()),
        ("pull", ()),
        ("up", ("decider",)),
        ("down", ("large",)),
        ("rollback", ("reasoning",)),
        ("prune", ()),
        ("install", (bundle,)),
        ("down", ("reasoning",)),
        ("down", ("decider",)),
        ("down", ("large",)),
    ]
    kwargs = [kw for _, _, kw in deploy.calls]
    assert kwargs[1]["allow_download"] is True
    assert all(kw["cfg"].profile == "local" for kw in kwargs)
    assert len({kw["actor"] for kw in kwargs}) == 1
    capsys.readouterr()
    assert _main(["--json", "deploy", "down"]) == 0
    assert set(_json(capsys)["data"]["classes"]) == {"reasoning", "decider", "large"}


def test_ut09_90_deploy_down_all_reports_each_class_and_propagates_failure(
    deploy: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `deploy down` without CLASS: per-class exit code and data; a failing class
    sets the exit code; an owner error stops the loop after recording the classes so far."""
    seen: list[str] = []

    def down(cls: str, **kw: Any) -> CommandResult:
        seen.append(cls)
        if cls == "large":
            msg = "compose down failed"
            raise ModelUnavailable(msg)
        return _ok({"stopped": cls}, code=1 if cls == "decider" else 0)

    monkeypatch.setattr(commands_deploy, "cmd_deploy_down", down)
    assert _main(["--json", "deploy", "down"]) == 1
    classes = _json(capsys)["data"]["classes"]
    assert classes["reasoning"] == {"exit_code": 0, "data": {"stopped": "reasoning"}}
    assert classes["decider"]["exit_code"] == 1
    assert classes["large"] == {"exit_code": 9, "error": "ModelUnavailable"}
    assert seen == ["reasoning", "decider", "large"]

    def failing(cls: str, **kw: Any) -> CommandResult:
        msg = "down"
        raise ModelUnavailable(msg)

    monkeypatch.setattr(commands_deploy, "cmd_deploy_down", failing)
    assert _main(["--json", "deploy", "down"]) == 9
    assert list(_json(capsys)["data"]["classes"]) == ["reasoning"]


def test_ut09_90_deploy_bad_class_or_bundle_is_usage_error(
    deploy: Recorder, tmp_path: Path
) -> None:
    """UT09-90 an unknown CLASS or a missing BUNDLE_DIR exits 2 before any owner call."""
    assert _main(["deploy", "up", "huge"]) == 2
    assert _main(["deploy", "down", "huge"]) == 2
    assert _main(["deploy", "install", str(tmp_path / "missing")]) == 2
    assert deploy.calls == []


# --- eval (UT09-90) ------------------------------------------------------------------------------


class Jobs:
    """``submit_job`` spy plus the queued status read of ``--no-wait``."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def submit(self, opts: object, **kw: Any) -> str:
        del opts
        self.calls.append(kw)
        return JOB

    def get(self, job_id: str) -> SimpleNamespace:
        assert job_id == JOB
        return SimpleNamespace(status="queued")


@pytest.fixture
def jobs(admin: Recorder, monkeypatch: pytest.MonkeyPatch) -> Jobs:
    spy = Jobs()
    monkeypatch.setattr(_eval_cmd, "submit_job", spy.submit)
    monkeypatch.setattr(cmd_admin, "submit_job", spy.submit)
    monkeypatch.setattr(_job_cmds, "jobs", spy)
    return spy


def test_ut09_90_eval_enqueues_built_payload(
    jobs: Jobs, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `eval` enqueues `build_eval_payload(**options)` with `eval_gpu_class` and
    `eval_idem_key`, priority None; it binds the eval deps factory first."""
    assert _main(["--json", "eval", "--ids", "G01, F01", "--depth", "fast", "--no-wait"]) == 0
    ((call,),) = [jobs.calls]
    assert call["kind"] == "eval"
    assert (call["gpu_class"], call["priority"], call["idem_key"]) == ("reasoning", None, None)
    assert call["inline"] is False
    payload = call["payload"]
    assert (payload["suite"], payload["profile"], payload["depth"]) == ("golden", "local", "fast")
    assert (payload["ids"], payload["repeat"], payload["no_judge"]) == (["G01", "F01"], 1, False)
    assert runner._Binding.factory is _eval_deps.make_deps
    assert _json(capsys)["data"] == {"schema": "cli/1", "job_id": JOB, "status": "queued"}


@pytest.mark.parametrize(
    ("args", "gpu", "idem"),
    [
        (["--suite", "classifier"], "decider", None),
        (["--mock-llm", "scripts"], "none", None),
        (["--resume", RUN, "--profile", "synth"], "reasoning", f"resume:eval:{RUN}"),
        (["--suite", "my.yaml", "--tags", "a,b", "--no-judge", "--repeat", "2",
          "--baseline", "base", "--set-baseline", "new-mock", "--compare", "hybrid"],
         "reasoning", None),
    ],
)  # fmt: skip
def test_ut09_90_eval_gpu_class_and_idem_key(
    jobs: Jobs, args: list[str], gpu: str, idem: str | None, tmp_path: Path
) -> None:
    """UT09-90 GPU class `decider` for `--suite classifier` (R-43), `none` with `--mock-llm`,
    `resume:eval:<run_id>` with `--resume`; paths are made absolute for the worker."""
    assert _main(["eval", *args, "--no-wait"]) == 0
    ((call,),) = [jobs.calls]
    assert (call["gpu_class"], call["idem_key"]) == (gpu, idem)
    payload = call["payload"]
    if "--mock-llm" in args:
        assert payload["mock_llm_dir"] == str(tmp_path / "scripts")
    if "my.yaml" in args:
        assert payload["suite"] == str(tmp_path / "my.yaml")
        assert (payload["tags"], payload["no_judge"], payload["repeat"]) == (["a", "b"], True, 2)
        assert (payload["baseline"], payload["set_baseline"]) == ("base", "new-mock")
        assert payload["compare_profile"] == "hybrid"
    if "--resume" in args:
        assert (payload["resume_run_id"], payload["profile"]) == (RUN, "synth")


@pytest.mark.parametrize(
    ("args", "ids"),
    [
        (["--ids", "G01,F01"], ["G01", "F01"]),
        (["--ids", "G01", "--ids", "F01"], ["G01", "F01"]),
        (["--ids", "G01, F01", "--ids", " ,G01", "--ids", "G03,"], ["G01", "F01", "G03"]),
    ],
)  # fmt: skip
def test_cv_t09_24_eval_ids_comma_repeated_and_mixed(
    jobs: Jobs, args: list[str], ids: list[str]
) -> None:
    """CV-T09-24 `--ids` takes comma lists, repeated options or both: blanks dropped,
    duplicates removed in first-seen order (T11-37 phase gate); `--tags` the same."""
    assert _main(["eval", *args, "--tags", "a,b", "--tags", "a", "--no-wait"]) == 0
    payload = jobs.calls[0]["payload"]
    assert (payload["ids"], payload["tags"]) == (ids, ["a", "b"])


def test_cv_t09_24_eval_unknown_id_is_config_error(
    jobs: Jobs, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-T09-24 an id the readable suite does not hold → ConfigError naming it (exit 3);
    nothing is enqueued. An unreadable suite is left to the runner (U11-66)."""
    g01 = {"id": "G01", "pipeline": "chat", "question": "How many incidents?",
           "expected": {"must_mention": ["t1"]}}  # fmt: skip
    suite = write_suite(tmp_path / "suite.yaml", [g01])
    assert _main(["--json", "eval", "--suite", str(suite), "--ids", "G01,G99", "--no-wait"]) == 3
    error = _json(capsys)["error"]
    assert (error["type"], error["message"]) == ("ConfigError", "unknown question ids: G99")
    assert jobs.calls == []
    assert _main(["eval", "--suite", str(suite), "--ids", "G01", "--no-wait"]) == 0
    assert _main(["eval", "--suite", "classifier", "--ids", "G99", "--no-wait"]) != 3
    assert _main(["eval", "--ids", "G99", "--no-wait"]) == 0  # golden file absent here


def test_ut09_90_eval_bad_option_is_config_error_and_nothing_enqueued(
    jobs: Jobs, cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 an option build_eval_payload rejects is ConfigError (3); nothing is enqueued;
    an unknown depth and `--inline --no-wait` are usage errors (2); a viewer is refused (11)."""
    assert _main(["--json", "eval", "--profile", "nope", "--no-wait"]) == 3
    assert _json(capsys)["error"]["type"] == "ConfigError"
    assert _main(["eval", "--depth", "huge", "--no-wait"]) == 2
    assert _main(["eval", "--inline", "--no-wait"]) == 2
    cli_env.user = "alice"
    assert _main(["eval", "--no-wait"]) == 11
    assert jobs.calls == []


def _outcome(status: str, *, result: dict[str, Any] | None = None, error: str | None = None,
             code: int = 0, detached: bool = False) -> FollowOutcome:  # fmt: skip
    last_error = None if error is None else {"class": error, "message": "boom"}
    job = JobRow(
        job_id=JOB,
        kind="eval",
        gpu_class="reasoning",
        status=status,
        priority=20,
        attempts=1,
        max_attempts=3,
        payload={},
        result=result,
        last_error=last_error,
    )
    return FollowOutcome(job, code, None, detached, False, ())


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (_outcome("done", result={"run_id": RUN, "passed": True, "exit_code": 0}), 0),
        (_outcome("done", result={"run_id": RUN, "passed": False, "exit_code": 14}), 14),
        (_outcome("done", result={"run_id": RUN, "passed": True}, error="ModelUnavailable"), 0),
        (_outcome("failed", error="ConfigError", code=3), 3),
        (_outcome("failed", error="ModelUnavailable", code=9), 1),
        (_outcome("canceled"), 1),
        (_outcome("running", code=130, detached=True), 130),
    ],
)  # fmt: skip
def test_ut09_90_eval_exit_code_from_job(
    jobs: Jobs, monkeypatch: pytest.MonkeyPatch, outcome: FollowOutcome, code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-90 a terminal eval job exits through `exit_code`: passed → 0, `passed = false` →
    14 (R-46), ConfigError → 3, any other failure or a cancel → 1; a detach keeps its code."""
    monkeypatch.setattr(_eval_cmd, "start_job", lambda *a, **kw: outcome)
    assert _main(["--json", "eval"]) == code
    payload = _json(capsys)
    assert payload["ok"] is (code == 0)
    if outcome.job.status == "done":
        assert payload["data"]["run_id"] == RUN
        assert payload["data"]["passed"] is outcome.job.result["passed"]  # type: ignore[index]
    if outcome.job.status in ("failed", "canceled"):
        assert payload["error"]["exit_code"] == code


def test_ut09_90_eval_compare_profile_result_and_human_output(
    jobs: Jobs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `--compare` prints `compare_run_id` next to `run_id` (T11-38)."""
    result = {"run_id": RUN, "passed": True, "exit_code": 0, "compare_run_id": RUN2}
    monkeypatch.setattr(_eval_cmd, "start_job", lambda *a, **kw: _outcome("done", result=result))
    assert _main(["eval", "--compare", "hybrid"]) == 0
    out = capsys.readouterr().out
    assert f"run_id: {RUN}" in out
    assert f"compare_run_id: {RUN2}" in out


@pytest.mark.parametrize(("exc", "code"), [(ModelUnavailable("gpu"), 1),
                                           (ConfigError("gpu lock held"), 3)])  # fmt: skip
def test_ut09_90_eval_inline_handler_error(
    jobs: Jobs, monkeypatch: pytest.MonkeyPatch, exc: Exception, code: int,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-90 `--inline`: a raised ConfigError → 3; any other HernessError → 1 (U11-69)."""

    def raise_(*args: Any, **kw: Any) -> None:
        assert kw["inline"] is True
        raise exc

    monkeypatch.setattr(_eval_cmd, "start_job", raise_)
    assert _main(["--json", "eval", "--inline"]) == code
    error = _json(capsys)["error"]
    assert (error["type"], error["exit_code"]) == (type(exc).__name__, code)
    assert _main(["eval", "--inline"]) == code
    assert any(line.startswith("Error: ") for line in capsys.readouterr().err.splitlines())


def test_ut09_90_eval_compare_runs_in_process(
    jobs: Jobs, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `--compare-runs A,B` renders through report.compare and enqueues nothing; a
    harness defect exits 14."""
    seen: list[tuple[list[str], Path]] = []
    defects: list[str] = []

    def compare(run_ids: list[str], *, data_root: Path) -> SimpleNamespace:
        seen.append((run_ids, data_root))
        return SimpleNamespace(newest_run_id=RUN2, run_ids=run_ids, harness_defects=defects)

    monkeypatch.setattr(herness.eval.report, "compare", compare)
    assert _main(["--json", "eval", "--compare-runs", f"{RUN},{RUN2}"]) == 0
    assert seen[0][0] == [RUN, RUN2]
    assert seen[0][1].name == "data"
    assert _json(capsys)["data"]["run_id"] == RUN2
    defects.append("G01")
    assert _main(["eval", "--compare-runs", f"{RUN},{RUN2}"]) == 14
    assert jobs.calls == []


# --- distill, laya -------------------------------------------------------------------------------


def test_ut09_90_distill_enqueues_decider_job(jobs: Jobs, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-90 `distill [--active]` enqueues `distill` with gpu_class decider and payload
    `{"active": bool}`; `--inline` registers the owner's handler in this process first."""
    assert _main(["distill", "--active", "--no-wait"]) == 0
    assert _main(["distill", "--no-wait"]) == 0
    assert [(c["kind"], c["payload"], c["gpu_class"]) for c in jobs.calls] == [
        ("distill", {"active": True}, "decider"),
        ("distill", {"active": False}, "decider"),
    ]
    registered: list[tuple[str, object]] = []
    handler = object()
    monkeypatch.setattr(_worker_boot, "cached_handlers", lambda: {"distill": handler})
    monkeypatch.setattr(herness.core.jobs.handlers, "register_handler",
                        lambda kind, h: registered.append((kind, h)))  # fmt: skip
    monkeypatch.setattr(cmd_admin, "start_job", lambda *a, **kw: _outcome("done"))
    assert _main(["distill", "--inline"]) == 0
    assert registered == [("distill", handler)]
    assert jobs.calls[-1]["inline"] is True


def test_ut09_90_laya_commands_call_spec03(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `laya status|accept|rollback` call laya_status, accept_model and rollback_model
    in-process with the actor's user_ref."""
    monkeypatch.setattr(
        laya_admin,
        "laya_status",
        admin.fake("status", {"current": "laya-20261001-1", "versions": []}),
    )
    monkeypatch.setattr(laya_admin, "accept_model", admin.fake("accept", 0))
    monkeypatch.setattr(laya_admin, "rollback_model", admin.fake("rollback", 0))
    assert _main(["--json", "laya", "status"]) == 0
    assert _json(capsys)["data"]["current"] == "laya-20261001-1"
    assert _main(["laya", "accept", "laya-20261002-1"]) == 0
    assert _main(["laya", "rollback", "laya-20261001-1"]) == 0
    _, accept, rollback = admin.calls
    assert accept[1] == ("laya-20261002-1",)
    assert rollback[1] == ("laya-20261001-1",)
    assert accept[2]["actor"] == rollback[2]["actor"]
    assert re.fullmatch(HASH_RE, accept[2]["actor"])


# --- privacy, maintenance ------------------------------------------------------------------------


def test_ut09_90_privacy_delete_and_inline_runs_each_job(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-90 `privacy delete --record-id ID... --reason-ref REF`: cmd_privacy_delete(ids,
    reason_ref, actor); with `--inline` each returned job runs through run_job_inline in
    order, after the maintenance handler is bound with the worker's memory_purge."""
    jobs_ = [{"record_id": "a:b:1", "job_id": "job_1"}, {"record_id": "a:b:2", "job_id": "job_2"}]

    def delete(ids: list[str], **kw: Any) -> CommandResult:
        admin.calls.append(("delete", (ids,), kw))
        return _ok({"jobs": [dict(j) for j in jobs_]}, warnings=["no worker is running"])

    ran: list[str] = []
    bound: list[object] = []

    def inline(opts: object, actor: object, job_id: str) -> SimpleNamespace:
        ran.append(job_id)
        return SimpleNamespace(job=SimpleNamespace(status="done"), exit_code=0)

    monkeypatch.setattr(commands_data, "cmd_privacy_delete", delete)
    monkeypatch.setattr(cmd_admin, "run_job_inline", inline)
    monkeypatch.setattr(herness.admin, "register_handlers",
                        lambda *, memory_purge: bound.append(memory_purge))  # fmt: skip
    args = ["privacy", "delete", "--record-id", "a:b:1", "--record-id", "a:b:2",
            "--reason-ref", "DSR-1"]  # fmt: skip
    assert _main(["--json", *args]) == 0
    ((_, (ids,), kw),) = admin.calls
    assert (ids, kw["reason_ref"]) == (["a:b:1", "a:b:2"], "DSR-1")
    assert re.fullmatch(HASH_RE, kw["actor"])
    assert "inline" not in kw
    payload = _json(capsys)
    assert payload["warnings"] == ["no worker is running"]
    assert (ran, bound) == ([], [])
    assert _main(["--json", *args, "--inline"]) == 0
    assert ran == ["job_1", "job_2"]
    assert bound == [_worker_boot.memory_purge]
    inline_payload = _json(capsys)
    assert [j["status"] for j in inline_payload["data"]["jobs"]] == ["done", "done"]
    assert inline_payload["warnings"] == []  # the jobs ran here (review M10)
    assert _main(["privacy", "delete", "--reason-ref", "DSR-1"]) == 2  # --record-id required


@pytest.mark.parametrize("action", ["backup", "purge"])
def test_ut09_90_maintenance_calls_cmd_maintenance(
    admin: Recorder, monkeypatch: pytest.MonkeyPatch, action: str,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-90 `maintenance backup|purge [--dry-run] [--inline]`: cmd_maintenance(action,
    dry_run, actor); `--inline` runs the returned job; a failed inline job sets the code."""
    monkeypatch.setattr(commands_data, "cmd_maintenance",
                        admin.fake("maintenance", _ok({"job_id": "job_9"})))  # fmt: skip
    monkeypatch.setattr(herness.admin, "register_handlers", lambda *, memory_purge: None)
    monkeypatch.setattr(cmd_admin, "run_job_inline", lambda opts, actor, job_id: SimpleNamespace(
        job=SimpleNamespace(status="failed"), exit_code=9))  # fmt: skip
    assert _main(["--json", "maintenance", action, "--dry-run"]) == 0
    ((_, args, kw),) = admin.calls
    assert (args, kw["dry_run"]) == ((action,), True)
    assert _json(capsys)["data"] == {"schema": "cli/1", "job_id": "job_9"}
    assert _main(["--json", "maintenance", action, "--inline"]) == 9
    assert _json(capsys)["data"]["status"] == "failed"
    assert admin.calls[-1][2]["dry_run"] is False


@pytest.mark.parametrize("path", NEW_COMMANDS)
def test_cv_t09_24_admin_commands_have_help(
    cli_env: CliEnv, path: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """CV-T09-24 every command of the card answers `--help` with exit 0."""
    del cli_env
    assert _main([*path.split(), "--help"]) == 0
    assert "Usage" in capsys.readouterr().out
