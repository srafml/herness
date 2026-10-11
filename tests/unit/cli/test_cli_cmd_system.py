"""Tests for herness._cli.cmd_system: init, status, ui, worker, gpu (T09-22, U09-93).

Commands run through ``herness.cli.main`` as the OS user ``alice`` (``cli_env``). ``ui``
never starts Streamlit: the module-level runner ``cmd_system._runner`` is replaced by a spy.
``worker`` fakes ``herness.core.jobs.run_worker``; ``gpu`` fakes ``request_gpu_class``.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
import typer
import typer.main
from tests.support.cli_env import CliEnv

from herness import cli
from herness._cli import _init, _status, cmd_system
from herness._cli.identity import COMMAND_ROLES
from herness.core import jobs
from herness.core import time as clock
from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

_CHILD_VARS = ("HERNESS_CONFIG_DIR", "HERNESS_DATA_DIR", "HERNESS_PROFILE",
               "HERNESS_SET_OVERRIDES")  # fmt: skip


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


def _json(capsys: pytest.CaptureFixture[str]) -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(capsys.readouterr().out)
    return loaded


@pytest.fixture
def admin(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> CliEnv:
    """``alice`` is an admin; the child-process variables are restored after the test."""
    cli_env.write_config(admins=["alice"])
    _isolate_child_env(monkeypatch)
    return cli_env


def _isolate_child_env(mp: pytest.MonkeyPatch) -> None:
    """Record the four child variables as absent, so ``worker``'s ``os.environ`` writes are
    undone at teardown (a bare ``delenv(raising=False)`` of an absent name records nothing)."""
    for name in _CHILD_VARS:
        mp.setenv(name, "")
        mp.delenv(name)


# --- registration (UT09-66 for the T09-22 paths) ----------------------------------------------


def _paths(group: Any, prefix: str = "") -> list[str]:
    out: list[str] = []
    for name, sub in getattr(group, "commands", {}).items():
        path = f"{prefix} {name}".strip()
        out += _paths(sub, path) if hasattr(sub, "commands") else [path]
    return out


def test_ut09_66_system_paths_registered_with_roles() -> None:
    """UT09-66 the six system commands are registered once, each with its U09-89 role;
    `gpu load` accepts `large` (R-47)."""
    cli._register_groups(cli.app)
    paths = set(_paths(typer.main.get_command(cli.app)))
    mine = {"init", "doctor", "status", "ui", "worker", "gpu load", "gpu unload"}
    assert mine <= paths
    roles = {path: COMMAND_ROLES[path] for path in mine}
    assert roles == {"init": "admin", "doctor": "viewer", "status": "viewer", "ui": "viewer",
                     "worker": "admin", "gpu load": "admin", "gpu unload": "admin"}  # fmt: skip
    names = [cli._entry_name(g) for g in cli.app.registered_groups]
    assert names.count("gpu") == 1
    assert "large" in cmd_system.GPU_LOAD_CLASSES


# --- init -------------------------------------------------------------------------------------


def _templates() -> list[str]:
    root = _init.TEMPLATE_DIR
    return sorted(p.relative_to(root).as_posix() for p in _init.template_files())


def test_ut09_86_init_bootstraps_then_is_idempotent(
    cli_env: CliEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-86 `init` without config (bootstrap, OI-05): data dirs, the shipped templates
    (OI-04), migrations, the next-step line; a second `init` copies, backs up and migrates
    nothing (T02-05)."""
    monkeypatch.setenv("HERNESS_PROFILE", "local")
    cfg, data = cli_env.root / "config", cli_env.root / "data"
    assert _main(["--config-dir", str(cfg), "--data-dir", str(data), "--json", "init"]) == 0
    first = _json(capsys)["data"]
    assert first["created_dirs"] == list(_init.DATA_SUBDIRS)
    assert all((data / d).is_dir() for d in _init.DATA_SUBDIRS)
    assert first["copied"] == _templates()
    assert {"herness.yaml", "injection_patterns.txt", "profiles/local.yaml"} <= set(first["copied"])
    assert first["backed_up"] == []
    assert first["migrations_applied"]
    assert (data / "ops.sqlite").is_file()
    shipped = (_init.TEMPLATE_DIR / "herness.yaml").read_bytes()
    assert (cfg / "herness.yaml").read_bytes() == shipped
    shipped_roles = (cfg / "herness.yaml").read_text(encoding="utf-8")
    roles = shipped_roles.replace("      admins: []", "      admins: [alice]", 1)
    (cfg / "herness.yaml").write_text(roles, encoding="utf-8")  # roles come from the config now
    assert _main(["--config-dir", str(cfg), "--data-dir", str(data), "init"]) == 0
    out = capsys.readouterr().out
    assert "Next: run `herness secrets init`." in out
    assert _main(["--config-dir", str(cfg), "--data-dir", str(data), "--json", "init"]) == 0
    second = _json(capsys)["data"]
    assert second == {"schema": "cli/1", "created_dirs": [], "copied": [], "backed_up": [],
                      "migrations_applied": []}  # fmt: skip


def test_ut09_86_init_force_backs_up_then_replaces(
    admin: CliEnv, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-86 `init --force`: each existing file is renamed `<name>.bak-<UTC stamp>` and
    replaced by the template; files absent before are copied."""
    stamp = datetime(2026, 10, 11, 8, 30, 5, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: stamp)
    cfg = admin.config_dir
    before = (cfg / "herness.yaml").read_bytes()
    args = ["--config-dir", str(cfg), "--data-dir", str(admin.root / "data"), "--json"]
    assert _main([*args, "init", "--force"]) == 0
    data = _json(capsys)["data"]
    backup = cfg / "herness.yaml.bak-20261011T083005Z"
    assert backup.read_bytes() == before
    assert "herness.yaml.bak-20261011T083005Z" in data["backed_up"]
    assert "profiles/local.yaml.bak-20261011T083005Z" in data["backed_up"]
    assert data["copied"] == _templates()
    assert (cfg / "herness.yaml").read_bytes() == (_init.TEMPLATE_DIR / "herness.yaml").read_bytes()


def test_ut09_86_init_without_force_keeps_existing(
    admin: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 `init` never clobbers an existing config file without `--force`."""
    cfg = admin.config_dir
    before = (cfg / "herness.yaml").read_bytes()
    args = ["--config-dir", str(cfg), "--data-dir", str(admin.root / "data"), "--json"]
    assert _main([*args, "init"]) == 0
    data = _json(capsys)["data"]
    assert (cfg / "herness.yaml").read_bytes() == before
    assert "herness.yaml" not in data["copied"]
    assert "metrics.yaml" not in data["copied"]  # write_full_config ships it
    assert "profiles/hybrid.yaml" not in data["copied"]
    assert data["backed_up"] == []


def test_ut09_86_copy_templates_edge_cases(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT09-86 copying onto the template directory itself is a no-op; a missing template
    tree is ConfigError; a backup name already taken is ConfigError (nothing lost)."""
    assert _init.copy_templates(_init.TEMPLATE_DIR, force=True) == ([], [])
    src = tmp_path / "tpl"
    (src / "profiles").mkdir(parents=True)
    (src / "herness.yaml").write_text("new", encoding="utf-8")
    (src / "profiles" / "local.yaml").write_text("p", encoding="utf-8")
    monkeypatch.setattr(_init, "TEMPLATE_DIR", src)
    dest = tmp_path / "cfg"
    dest.mkdir()
    (dest / "herness.yaml").write_text("old", encoding="utf-8")
    stamp = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    monkeypatch.setattr(clock, "now", lambda: stamp)
    (dest / "herness.yaml.bak-20260102T030405Z").write_text("older", encoding="utf-8")
    with pytest.raises(ConfigError, match="backup"):
        _init.copy_templates(dest, force=True)
    assert (dest / "herness.yaml").read_text(encoding="utf-8") == "old"
    monkeypatch.setattr(_init, "TEMPLATE_DIR", tmp_path / "absent")
    with pytest.raises(ConfigError, match="templates"):
        _init.copy_templates(dest, force=False)


# --- status -----------------------------------------------------------------------------------


def _fake_status(monkeypatch: pytest.MonkeyPatch, build: _status.BuildView | None) -> None:
    from herness.store.ops import ui_reads  # noqa: PLC0415 - patched where used

    job = SimpleNamespace(model_dump=lambda **kw: {"job_id": "j1", "status": "done"})
    snapshot = {"workers": [{"worker_id": "w1"}], "running": [], "queue": {"queued": 2},
                "planned_rekey": None, "breakers": [], "faults_enabled": False,
                "gpu": {}, "failed_24h": 0}  # fmt: skip
    monkeypatch.setattr(_status, "current_build", lambda now: build)
    monkeypatch.setattr(_status, "_bind_chains", lambda cfg: None)
    monkeypatch.setattr(_status, "dq_failures", lambda build_id: [{"check_name": "c1"}])
    monkeypatch.setattr(jobs, "list_jobs", lambda **kw: [job] if kw["kind"] else [])
    monkeypatch.setattr(jobs, "status_snapshot", lambda now: snapshot)
    monkeypatch.setattr(jobs, "chat_policy", lambda now: "defer")
    monkeypatch.setattr(ui_reads, "ui_list_runs", lambda limit: [{"run_id": "r1"}] * limit)


def test_ut09_86_status_sections(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 `status`: current build and age, last build job, DQ warnings, the jobs
    snapshot keys, the last five runs and the chat mode."""
    _fake_status(monkeypatch, _status.BuildView("b1", 3.5))
    assert _main(["--config-dir", str(admin.config_dir), "--json", "status"]) == 0
    data = _json(capsys)["data"]
    assert data["current_build"] == {"build_id": "b1", "age_h": 3.5}
    assert data["last_build_job"] == {"job_id": "j1", "status": "done"}
    assert data["dq_warnings"] == [{"check_name": "c1"}]
    assert data["jobs"] == {"workers": [{"worker_id": "w1"}], "running": [],
                            "queue": {"queued": 2}, "planned_rekey": None, "breakers": [],
                            "faults_enabled": False}  # fmt: skip
    assert data["runs"] == [{"run_id": "r1"}] * 5
    assert data["chat_mode"] == "defer"
    assert _main(["--config-dir", str(admin.config_dir), "status"]) == 0
    out = capsys.readouterr().out
    assert "Current build: b1, age 3.5 h" in out
    assert "chat_mode: defer" in out


def test_ut09_86_status_without_current(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 `status` without CURRENT: "No promoted warehouse yet." and the rest."""
    _fake_status(monkeypatch, None)
    monkeypatch.setattr(jobs, "list_jobs", lambda **kw: [])
    assert _main(["--config-dir", str(admin.config_dir), "status"]) == 0
    captured = capsys.readouterr()
    assert "No promoted warehouse yet." in captured.out
    assert "last_build_job: null" in captured.out
    assert "dq_warnings: []" in captured.out


def test_ut09_86_status_without_model_clients(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 a profile whose model clients cannot be built: the jobs and chat sections are
    null with a warning; the other sections still print, exit 0."""
    _fake_status(monkeypatch, None)
    monkeypatch.setattr(_status, "_bind_chains", lambda cfg: "chat mode unavailable")
    assert _main(["--config-dir", str(admin.config_dir), "--json", "status"]) == 0
    envelope = _json(capsys)
    assert (envelope["data"]["jobs"], envelope["data"]["chat_mode"]) == (None, None)
    assert envelope["warnings"] == ["chat mode unavailable"]
    assert envelope["data"]["runs"] == [{"run_id": "r1"}] * 5


def test_ut09_86_bind_chains_reports_config_error(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-86 `_bind_chains` binds the registry, or names the ConfigError class."""
    from herness.core import resilience  # noqa: PLC0415 - patched where used
    from herness.harness.llm import registry  # noqa: PLC0415

    cfg = cli.GlobalOptions(config_dir=admin.config_dir).config()
    bound: list[object] = []
    monkeypatch.setattr(resilience, "bind_chain_registry", bound.append)
    monkeypatch.setattr(registry, "LLMRegistry", lambda models, profile: "llms")
    assert _status._bind_chains(cfg) is None
    assert bound == ["llms"]

    def refuse(models: object, profile: str) -> None:
        msg = "off-network client"
        raise ConfigError(msg)

    monkeypatch.setattr(registry, "LLMRegistry", refuse)
    assert _status._bind_chains(cfg) == "chat mode and job status unavailable (ConfigError)"


def test_ut09_86_build_age_unknown_is_shown(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 a current build without a finished time shows age unknown."""
    _fake_status(monkeypatch, _status.BuildView("b9", None))
    assert _main(["--config-dir", str(admin.config_dir), "status"]) == 0
    assert "Current build: b9, age unknown" in capsys.readouterr().out


# --- ui ---------------------------------------------------------------------------------------


class Runner:
    """Spy for ``subprocess.run``: records argv and env, returns a fixed code."""

    def __init__(self, code: int = 0) -> None:
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.code = code

    def __call__(self, args: list[str], **kw: Any) -> SimpleNamespace:
        self.calls.append((args, kw))
        return SimpleNamespace(returncode=self.code)


@pytest.fixture
def runner(monkeypatch: pytest.MonkeyPatch) -> Runner:
    spy = Runner()
    monkeypatch.setattr(cmd_system, "_runner", spy)
    return spy


def test_ut09_86_ui_exact_argv_and_env(admin: CliEnv, runner: Runner) -> None:
    """UT09-86 `ui`: exact argument list and environment; the child's code is returned."""
    runner.code = 3
    args = ["--config-dir", str(admin.config_dir), "--set", "logging.level=DEBUG", "ui"]
    assert _main(args) == 3
    (argv, kw), = runner.calls  # fmt: skip
    assert argv == [
        sys.executable, "-m", "streamlit", "run", str(cmd_system.APP_HOME),
        "--server.address", "127.0.0.1", "--server.port", "8501",
        "--server.headless", "true", "--browser.gatherUsageStats", "false",
        "--server.enableXsrfProtection", "true", "--server.enableCORS", "true",
    ]  # fmt: skip
    assert cmd_system.APP_HOME.is_file()
    assert kw["check"] is False
    assert "timeout" not in kw
    env = kw["env"]
    data_dir = (admin.root / "data").resolve()
    assert env["HERNESS_CONFIG_DIR"] == str(admin.config_dir.resolve())
    assert Path(env["HERNESS_DATA_DIR"]) == data_dir
    assert env["HERNESS_PROFILE"] == "local"
    assert json.loads(env["HERNESS_SET_OVERRIDES"]) == ["logging.level=DEBUG"]
    assert (env["HF_HUB_OFFLINE"], env["DO_NOT_TRACK"]) == ("1", "1")
    assert env["PATH"] == os.environ["PATH"]
    assert "HERNESS_CONFIG_DIR" not in os.environ  # only the child's copy


def test_ut09_86_ui_port_override_and_range(admin: CliEnv, runner: Runner) -> None:
    """UT09-86 `--port` overrides the configured port for this launch; outside
    1024-65535 is a usage error and nothing starts."""
    assert _main(["--config-dir", str(admin.config_dir), "ui", "--port", "9100"]) == 0
    assert runner.calls[0][0][runner.calls[0][0].index("--server.port") + 1] == "9100"
    for bad in ("80", "70000"):
        assert _main(["--config-dir", str(admin.config_dir), "ui", "--port", bad]) == 2
    assert len(runner.calls) == 1


@pytest.mark.parametrize("bind", ["127.0.0.1", "::1", "localhost"])
def test_ut09_86_ui_args_accept_loopback(bind: str) -> None:
    """UT09-86 the three loopback binds build the argument list."""
    assert cmd_system.ui_args(bind, 8501)[6] == bind


@pytest.mark.parametrize("bind", ["0.0.0.0", "192.168.1.10", "::", "127.0.0.2"])  # noqa: S104
def test_ut09_86_ui_non_loopback_refused(bind: str) -> None:
    """UT09-86 a non-loopback bind is ConfigError "dashboard bind must be loopback"."""
    with pytest.raises(ConfigError, match="dashboard bind must be loopback"):
        cmd_system.ui_args(bind, 8501)


# --- worker -----------------------------------------------------------------------------------


class WorkerSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.env: dict[str, str | None] = {}

    def __call__(self, **kw: Any) -> int:
        self.calls.append(kw)
        self.env = {name: os.environ.get(name) for name in _CHILD_VARS}
        return 0


@pytest.fixture
def worker_spy(monkeypatch: pytest.MonkeyPatch) -> WorkerSpy:
    spy = WorkerSpy()
    monkeypatch.setattr(jobs, "run_worker", spy)
    return spy


def test_ut09_86_worker_defaults_and_env(admin: CliEnv, worker_spy: WorkerSpy) -> None:
    """UT09-86 `worker`: the four child variables are set, then `run_worker` gets all four
    GPU classes, no concurrency, `once` false and the CLI bootstrap (U09-104)."""
    args = ["--config-dir", str(admin.config_dir), "--profile", "hybrid", "--set",
            "logging.level=DEBUG", "worker"]  # fmt: skip
    assert _main(args) == 0
    assert worker_spy.calls == [{"gpu_classes": ["none", "reasoning", "decider", "large"],
                                 "concurrency": None, "once": False,
                                 "bootstrap": "herness.cli:worker_bootstrap"}]  # fmt: skip
    env = worker_spy.env
    assert env["HERNESS_CONFIG_DIR"] == str(admin.config_dir.resolve())
    assert env["HERNESS_PROFILE"] == "hybrid"
    assert Path(str(env["HERNESS_DATA_DIR"])) == (admin.root / "data").resolve()
    assert json.loads(str(env["HERNESS_SET_OVERRIDES"])) == ["logging.level=DEBUG"]


def test_ut09_86_worker_options(admin: CliEnv, worker_spy: WorkerSpy) -> None:
    """UT09-86 `--gpu-class` subset (order kept, duplicates dropped), `--concurrency`,
    `--once`; the worker's exit code is returned."""
    args = ["--config-dir", str(admin.config_dir), "worker", "--gpu-class", "decider, none,decider",
            "--concurrency", "4", "--once"]  # fmt: skip
    assert _main(args) == 0
    call = worker_spy.calls[0]
    assert (call["gpu_classes"], call["concurrency"], call["once"]) == (
        ["decider", "none"],
        4,
        True,
    )


@pytest.mark.parametrize(
    "extra",
    [
        ["--gpu-class", "huge"],
        ["--gpu-class", ""],
        ["--concurrency", "0"],
        ["--concurrency", "65"],
    ],
)
def test_ut09_86_worker_bad_options_are_usage_errors(
    admin: CliEnv, worker_spy: WorkerSpy, extra: list[str]
) -> None:
    """UT09-86 an unknown GPU class or a concurrency outside 1-64: exit 2, no worker, no
    environment change."""
    assert _main(["--config-dir", str(admin.config_dir), "worker", *extra]) == 2
    assert worker_spy.calls == []
    assert "HERNESS_CONFIG_DIR" not in os.environ


def test_ut09_86_worker_exit_code_passes_through(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-86 a worker ending with exit 1 makes the command exit 1."""
    monkeypatch.setattr(jobs, "run_worker", lambda **kw: 1)
    assert _main(["--config-dir", str(admin.config_dir), "worker", "--once"]) == 1


# --- gpu --------------------------------------------------------------------------------------


@pytest.mark.parametrize(("args", "cls"), [(["load", "large"], "large"),
                                           (["load", "reasoning"], "reasoning"),
                                           (["unload"], "none")])  # fmt: skip
def test_ut09_86_gpu_requests_class(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str],
    args: list[str], cls: str,
) -> None:  # fmt: skip
    """UT09-86 `gpu load CLASS` / `gpu unload` call `request_gpu_class` (none for unload)."""
    seen: list[str] = []
    monkeypatch.setattr(jobs, "request_gpu_class", lambda c: seen.append(c) or "requested")
    assert _main(["--config-dir", str(admin.config_dir), "--json", "gpu", *args]) == 0
    assert seen == [cls]
    assert _json(capsys)["data"]["requested_class"] == cls


def test_ut09_86_gpu_no_worker_exit_1(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 no live worker: exit 1 with "No worker is running." and the fix."""
    monkeypatch.setattr(jobs, "request_gpu_class", lambda c: "no_worker")
    assert _main(["--config-dir", str(admin.config_dir), "gpu", "load", "decider"]) == 1
    err = capsys.readouterr().err
    assert "Error: No worker is running." in err
    assert "Fix: Start `herness worker`, or use `herness deploy up CLASS`." in err
    assert _main(["--config-dir", str(admin.config_dir), "--json", "gpu", "unload"]) == 1
    error = _json(capsys)["error"]
    assert (error["exit_code"], error["message"]) == (1, "No worker is running.")


def test_ut09_86_gpu_bad_class_is_usage_error(
    admin: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-86 `gpu load none` or an unknown class: exit 2, nothing requested."""
    seen: list[str] = []
    monkeypatch.setattr(jobs, "request_gpu_class", seen.append)
    for cls in ("none", "huge"):
        assert _main(["--config-dir", str(admin.config_dir), "gpu", "load", cls]) == 2
    assert seen == []


def test_ut09_86_viewer_refused_for_admin_system_commands(
    cli_env: CliEnv, worker_spy: WorkerSpy, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-86 a viewer cannot run `worker`, `gpu load` or `init` (exit 11)."""
    cli_env.write_config(admins=["root"])
    cfg = str(cli_env.config_dir)
    for args in (["worker"], ["gpu", "load", "large"], ["init"]):
        assert _main(["--config-dir", cfg, *args]) == 11
    capsys.readouterr()
    assert worker_spy.calls == []


def test_ut09_86_init_data_root_resolution(admin: CliEnv, tmp_path: Path) -> None:
    """UT09-86 `init`'s data root: `--data-dir`, else the loaded `paths.data`, else `data`
    beside the config directory (the shipped value, resolved as `load_config` does)."""
    with_cfg = cli.GlobalOptions(config_dir=admin.config_dir)
    assert _init._data_root(with_cfg) == (admin.root / "data").resolve()
    bare = cli.GlobalOptions(config_dir=tmp_path / "fresh" / "config")
    assert _init._data_root(bare) == (tmp_path / "fresh").resolve() / "data"
    given = cli.GlobalOptions(config_dir=tmp_path, data_dir=tmp_path / "d")
    assert _init._data_root(given) == (tmp_path / "d").resolve()


class _Con:
    def __init__(self, row: tuple[object, ...] | None) -> None:
        self.row = row

    def __enter__(self) -> _Con:
        return self

    def __exit__(self, *exc: object) -> None:
        return None

    def execute(self, sql: str) -> _Con:
        assert sql.startswith("SELECT")
        return self

    def fetchone(self) -> tuple[object, ...] | None:
        return self.row


@pytest.mark.parametrize(("row", "age"), [(None, None), ((None,), None), ((0,), None)])
def test_ut09_86_current_build_age(
    monkeypatch: pytest.MonkeyPatch, row: tuple[object, ...] | None, age: float | None
) -> None:
    """UT09-86 the CURRENT reader: no `meta.build` row or no `finished_at` -> age unknown;
    an epoch-microsecond `finished_at` -> hours to one decimal."""
    from herness.store import warehouse  # noqa: PLC0415 - patched where used

    monkeypatch.setattr(warehouse, "read_current", lambda: "b1")
    monkeypatch.setattr(warehouse, "open_readonly", lambda build_id: _Con(row))
    now = datetime(1970, 1, 1, 3, 30, tzinfo=UTC)
    view = _status.current_build(now)
    expected = 3.5 if row == (0,) else age
    assert view == _status.BuildView("b1", expected)


def test_cv_t09_22_worker_env_restored(cli_env: CliEnv, worker_spy: WorkerSpy) -> None:
    """CV T09-22 `worker` writes the child variables into os.environ; the isolation the
    worker tests use restores the values seen before the test once it is undone."""
    cli_env.write_config(admins=["alice"])
    before = {name: os.environ.get(name) for name in _CHILD_VARS}
    with pytest.MonkeyPatch.context() as mp:
        _isolate_child_env(mp)
        assert _main(["--config-dir", str(cli_env.config_dir), "worker", "--once"]) == 0
        assert os.environ["HERNESS_PROFILE"] == "local"
        assert worker_spy.env["HERNESS_PROFILE"] == "local"
    assert {name: os.environ.get(name) for name in _CHILD_VARS} == before
