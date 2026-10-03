"""Tests for herness.cli: main, global options and start-up validation (T09-20)."""

from __future__ import annotations

import importlib.metadata
import json
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
import typer
from structlog.testing import capture_logs
from tests.support.cli_env import CliEnv

from herness import cli
from herness.core import config_validate as cv
from herness.core import errors as e
from herness.core.config_view import ConfigIssue
from herness.core.resilience import ProcessState
from herness.harness.memory.types import MemoryNotFound

pytestmark = pytest.mark.unit

Handler = Callable[[], object]


@pytest.fixture
def probe(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Order log of guard installs and handler runs; restores the app's command list."""
    events: list[str] = []
    real = cli.install_socket_guard

    def guard(cfg: object) -> None:
        events.append("guard")
        real(cfg)  # type: ignore[arg-type]

    monkeypatch.setattr(cli, "install_socket_guard", guard)
    saved = list(cli.app.registered_commands)
    try:
        yield events
    finally:
        cli.app.registered_commands[:] = saved


def _command(handler: Handler, name: str = "probe") -> None:
    cli.app.command(name)(handler)


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    code = exc.value.code
    assert isinstance(code, int)
    return code


def _raiser(exc: BaseException, events: list[str]) -> Handler:
    def handler() -> None:
        events.append("handler")
        raise exc

    return handler


def _circuit(key: str) -> e.CircuitOpen:
    return e.CircuitOpen("open", key=key, retry_at=datetime(2026, 9, 1, tzinfo=UTC))


_CASES = [
    (e.ConfigError("c"), 3),
    (e.AuthError("a"), 4),
    (_circuit("jira"), 4),
    (e.SchemaViolation("s"), 5),
    (MemoryNotFound("memory", "mem_1"), 7),
    (e.StoreBusy("b"), 8),
    (_circuit("model:x"), 9),
    (e.BudgetExceeded("x"), 10),
    (e.PermissionDenied("p"), 11),
    (e.ReportContractError("r"), 12),
    (e.EgressBlocked("eg"), 13),
    (e.PolicyViolation("pv"), 1),
    (KeyboardInterrupt(), 130),
]


@pytest.mark.parametrize(("exc", "code"), _CASES, ids=lambda v: type(v).__name__)
def test_ut09_95_main_exit_codes(
    probe: list[str], exc: BaseException, code: int, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-95 a handler raising each class exits with its R-46 code; guard installed first."""
    _command(_raiser(exc, probe))
    assert _main(["--config-dir", "missing-dir", "probe"]) == code
    assert probe == ["guard", "handler"]
    out, err = capsys.readouterr()
    assert out == ""
    errors = [line for line in err.splitlines() if line.startswith("Error: ")]
    assert len(errors) == (0 if code == 130 else 1)


def test_ut09_95_json_envelope_on_error(
    probe: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-95 with --json an error is one JSON envelope on stdout."""
    _command(_raiser(e.StoreBusy("busy"), probe))
    assert _main(["--json", "probe"]) == 8
    out = capsys.readouterr().out
    assert out.count("\n") == 1
    payload = json.loads(out)
    assert (payload["ok"], payload["error"]["type"], payload["error"]["exit_code"]) == (
        False,
        "StoreBusy",
        8,
    )


def test_ut09_95_unexpected_exception(
    probe: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-95 any other exception: `cli.command.failed`, InternalError envelope, exit 1."""
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)  # keep capture_logs
    _command(_raiser(ValueError("password=hunter2"), probe))
    with capture_logs() as logs:
        assert _main(["--json", "probe"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["type"] == "InternalError"
    assert payload["error"]["message"] == "Unexpected error."
    failed = [x for x in logs if x["event"] == "cli.command.failed"]
    assert failed == [
        {
            "component": "cli",
            "event": "cli.command.failed",
            "log_level": "error",
            "command": "herness",
            "error_type": "ValueError",
        }
    ]
    assert "hunter2" not in json.dumps(logs, default=str)


def test_ut09_95_returned_code_metric_and_log(
    probe: list[str], reset_process_state: ProcessState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-95 a returned int is the exit code; completion is logged and counted."""
    monkeypatch.setattr(cli, "configure_logging", lambda *a, **k: None)  # keep capture_logs
    _command(lambda: 6)
    with capture_logs() as logs:
        assert _main(["probe"]) == 6
    (done,) = [x for x in logs if x["event"] == "cli.command.completed"]
    assert (done["command"], done["exit_code"]) == ("herness", 6)
    assert isinstance(done["duration_ms"], int)
    key = ("herness_cli_commands_total", (("command", "herness"), ("exit_code", "6")), "cli")
    assert reset_process_state.metric_buffer.counters[key] == 1.0
    _command(lambda: None, "nothing")
    assert _main(["nothing"]) == 0


def test_ut09_95_usage_errors(probe: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-95 unknown option, unknown command and no arguments exit 2 (R-46)."""
    _command(lambda: 0)
    assert _main(["--nope", "probe"]) == 2
    assert "No such option" in capsys.readouterr().err
    assert _main(["--json", "nosuch"]) == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["exit_code"] == 2
    assert _main([]) == 2
    assert "Usage" in "".join(capsys.readouterr())


def test_ut09_95_bootstrap_error_exit_3(
    probe: list[str], capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-95 an unknown --profile fails the bootstrap load: exit 3, no handler runs."""
    _command(_raiser(ValueError("x"), probe))
    assert _main(["--profile=bogus", "probe"]) == 3
    assert probe == []
    assert "Error: unknown profile: bogus" in capsys.readouterr().err.splitlines()


def test_ut09_95_prescan_forms(cli_env: CliEnv) -> None:
    """UT09-95 pre-scan of --config-dir/--profile: first occurrence, `=` and space forms."""
    args = ["--config-dir=a", "--profile", "synth", "--config-dir", "b", "--profile=local"]
    assert cli._prescan(args, "--config-dir") == "a"
    assert cli._prescan(args, "--profile") == "synth"
    assert cli._prescan(["--profile"], "--profile") is None
    assert cli._prescan([], "--config-dir") is None


def test_ut09_95_bootstrap_uses_config_dir(cli_env: CliEnv) -> None:
    """UT09-95 an existing herness.yaml is read; an absent one gives the secure default."""
    cfg = cli._bootstrap(cli_env.write_config(), "synth")
    assert (cfg.profile, cfg.security.data_policy.approved_by) == ("synth", "ops-lead")
    default = cli._bootstrap(cli_env.root / "nowhere", None)
    assert (default.profile, default.security.egress.enabled, default.source_hosts) == (
        "local",
        False,
        (),
    )


def test_ut09_95_version_and_help(cli_env: CliEnv, capsys: pytest.CaptureFixture[str]) -> None:
    """UT09-95 --version prints `herness <version>` without config; --help exits 0."""
    assert _main(["--version"]) == 0
    assert capsys.readouterr().out == f"herness {importlib.metadata.version('herness')}\n"
    assert _main(["--help"]) == 0
    assert "--config-dir" in capsys.readouterr().out


# --- UT09-85 global options ----------------------------------------------------------------------


@pytest.mark.parametrize(
    "args",
    [
        ["--quiet", "--verbose", "probe"],
        ["--set", "nodots=1", "probe"],
        ["--set", "Upper.case=1", "probe"],
        ["--set", "a.b", "probe"],
    ],
)
def test_ut09_85_usage_errors(probe: list[str], args: list[str]) -> None:
    """UT09-85 --quiet with --verbose, or a malformed --set, is a usage error (exit 2)."""
    _command(_raiser(ValueError("x"), probe))
    assert _main(args) == 2
    assert "handler" not in probe


def test_ut09_85_options_reach_ctx_obj(probe: list[str]) -> None:
    """UT09-85 the callback fills the shared GlobalOptions; config is not loaded."""
    seen: list[cli.GlobalOptions] = []

    def handler(ctx: typer.Context) -> int:
        seen.append(ctx.obj)
        return 0

    _command(handler)
    args = ["--data-dir", "d", "--set", "a.b_c=1", "--set", "x.y=[1]", "--quiet", "probe"]
    assert _main(["--profile", "synth", *args]) == 0
    (opts,) = seen
    assert (opts.data_dir, opts.profile, opts.set_overrides) == (
        Path("d"),
        "synth",
        ("a.b_c=1", "x.y=[1]"),
    )
    assert (opts.quiet, opts.verbose, opts.json, opts._cfg) == (True, False, False, None)


def test_ut09_85_config_cached_with_data_dir(cli_env: CliEnv) -> None:
    """UT09-85 config(): `--data-dir` becomes `paths.data`; cached per instance; actor built."""
    cli_env.write_config(admins=["alice"])
    opts = cli.GlobalOptions(config_dir=cli_env.config_dir, data_dir=cli_env.root / "elsewhere")
    cfg = opts.config()
    assert Path(cfg.paths.data) == cli_env.root / "elsewhere"
    assert opts.config() is cfg
    assert opts.actor(need_ref=True).role == "admin"


# --- UT09-103 start-up validation ----------------------------------------------------------------


def _fake_hook(monkeypatch: pytest.MonkeyPatch, issues: list[ConfigIssue]) -> None:
    monkeypatch.setattr(cv, "run_owner_validators", lambda cfg, *, offline: list(issues))


ERROR = ConfigIssue("error", "metrics.metrics.x", "unknown metric", "metrics.yaml")
WARN = ConfigIssue("warn", "weights", "previous snapshot unreadable", "weights.yaml")


def test_ut09_103_error_issue_raises(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-103 an error issue -> ConfigError with string details; raise_on_error=False returns."""
    cfg = cli.GlobalOptions(config_dir=cli_env.write_config()).config(startup_validation=False)
    _fake_hook(monkeypatch, [ERROR, WARN])
    with capture_logs() as logs, pytest.raises(e.ConfigError) as exc:
        cli.run_startup_validation(cfg)
    assert exc.value.message == "start-up validation failed: 1 errors"
    assert exc.value.details["issues"] == str(ERROR)
    assert {"errors": 1, "warnings": 1}.items() <= logs[-1].items()
    assert logs[-1]["event"] == "config.startup.validated"
    assert cli.run_startup_validation(cfg, raise_on_error=False) == [ERROR, WARN]
    _fake_hook(monkeypatch, [WARN])
    assert cli.run_startup_validation(cfg) == [WARN]


def test_ut09_103_registered_once(cli_env: CliEnv) -> None:
    """UT09-103 the owner validators are registered once; a second call adds nothing."""
    cfg = cli.GlobalOptions(config_dir=cli_env.write_config()).config(startup_validation=False)
    issues = cli.run_startup_validation(cfg, config_dir=cli_env.config_dir)
    assert [i for i in issues if i.severity == "error"] == []
    first = dict(cv._VALIDATORS)
    assert set(first) == {"metrics.catalog", "resilience", "enrich.deciders"}
    again = cli.run_startup_validation(cfg, offline=False, config_dir=cli_env.config_dir)
    assert again == issues
    assert first == cv._VALIDATORS


def test_ut09_103_metrics_adapter_file_errors(cli_env: CliEnv) -> None:
    """UT09-103 metrics.catalog adapter: missing, oversized or invalid file -> one error issue."""
    cfg_dir = cli_env.write_config()
    cfg = cli.GlobalOptions(config_dir=cfg_dir).config(startup_validation=False)
    adapter = cli._METRICS_CATALOG
    adapter.config_dir = cfg_dir
    shipped = adapter(cfg, offline=True)
    assert shipped  # the shipped catalog has warn issues only (impl 04 U04-26)
    assert {i.severity for i in shipped} == {"warn"}
    metrics = cfg_dir / "metrics.yaml"
    for content in (b"metrics: [unclosed", b"version: 1\nbogus: 1\n", b"x" * (1_048_576 + 1)):
        metrics.write_bytes(content)
        (issue,) = adapter(cfg, offline=True)
        assert (issue.severity, issue.path, issue.file) == ("error", "metrics", "metrics.yaml")
    metrics.unlink()
    (issue,) = adapter(cfg, offline=True)
    assert issue.message == "metric catalog file is missing, too large or invalid"


def test_ut09_103_global_options_hook(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-103 config() runs the hook once; startup_validation=False skips it."""
    calls: list[Path] = []
    monkeypatch.setattr(
        cli, "run_startup_validation", lambda cfg, *, config_dir: calls.append(config_dir) or []
    )
    opts = cli.GlobalOptions(config_dir=cli_env.write_config())
    opts.config(startup_validation=False)
    assert calls == []
    opts.config()
    opts.config()
    assert calls == [cli_env.config_dir]


def test_ut09_103_cli_exit_3(
    cli_env: CliEnv,
    probe: list[str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """UT09-103 a start-up error issue makes the command exit 3 (R-46); a warn issue does not."""

    def handler(ctx: typer.Context) -> int:
        ctx.obj.config()
        return 0

    _command(handler)
    cfg_dir = str(cli_env.write_config())
    _fake_hook(monkeypatch, [ERROR])
    assert _main(["--config-dir", cfg_dir, "--json", "probe"]) == 3
    payload = json.loads(capsys.readouterr().out)
    assert payload["error"]["type"] == "ConfigError"
    _fake_hook(monkeypatch, [WARN])
    assert _main(["--config-dir", cfg_dir, "probe"]) == 0
