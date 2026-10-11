"""Tests for herness._cli.doctor: ``run_doctor``, ``DoctorCheckResult`` and ``herness doctor``
(T09-22, U09-93, U09-94).

Every owner the checks call is faked on its own module (the checks import them lazily): the
CURRENT reader, the migrations, the start-up hook, the three store health functions, the
worker liveness, ``swarm_health``, the report templates and the spec 10 host rows. The config
is the real ``cli_env`` tree, so the ``config`` row loads it.
"""

from __future__ import annotations

import json
import sys
import types
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest
from tests.support.cli_env import CliEnv

from herness import cli
from herness._cli import _status, doctor
from herness._cli.doctor import DoctorCheckResult, run_doctor
from herness.admin import doctor_host
from herness.admin.doctor_host import HostCheckResult
from herness.connectors.settings import SourcesConfig
from herness.core import jobs
from herness.core.config_view import ConfigIssue
from herness.core.errors import StoreBusy
from herness.harness.swarm import lifecycle
from herness.reports import render
from herness.store import (
    ops,  # the package re-exports the owner functions
    vectors,
    warehouse,
)

pytestmark = pytest.mark.unit

HOST_ROWS = [
    HostCheckResult("python_uv", "PASS", "3.12, uv", ""),
    HostCheckResult("disk_free", "WARN", "40 GB free", "Free space"),
]


class Fakes:
    """The owners' answers; each test changes the ones it needs."""

    def __init__(self) -> None:
        self.build: _status.BuildView | None = _status.BuildView("b1", 2.0)
        self.pending: list[str] = []
        self.issues: list[ConfigIssue] = []
        self.ops: tuple[str, str] = ("ok", "")
        self.warehouse: tuple[str, str] = ("ok", "")
        self.vectors: tuple[str, str] = ("ok", "")
        self.alive = True
        self.swarm: Callable[..., dict[str, str]] = lambda **kw: {"status": "ok", "reason": ""}
        self.templates: tuple[str, str] = ("ok", "templates loaded")
        self.host: list[HostCheckResult] = list(HOST_ROWS)
        self.validation_calls: list[dict[str, Any]] = []
        self.host_config_dirs: list[Path] = []
        self.warehouse_calls: list[dict[str, Any]] = []


@pytest.fixture
def fakes(cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch) -> Fakes:
    """``cli_env`` config plus every doctor owner faked to a healthy answer."""
    cli_env.write_config(admins=["alice"])
    f = Fakes()

    def validation(cfg: object, **kw: Any) -> list[ConfigIssue]:
        del cfg
        f.validation_calls.append(kw)
        return f.issues

    def wh_health(**kw: Any) -> tuple[str, str]:
        f.warehouse_calls.append(kw)
        return f.warehouse

    def host(cfg: object) -> list[HostCheckResult]:
        del cfg
        f.host_config_dirs.append(doctor_host._config_dir())
        return f.host

    monkeypatch.setattr(_status, "current_build", lambda now: f.build)
    monkeypatch.setattr(ops, "pending_migrations", lambda: f.pending)
    monkeypatch.setattr(cli, "run_startup_validation", validation)
    monkeypatch.setattr(ops, "ops_health", lambda: f.ops)
    monkeypatch.setattr(warehouse, "warehouse_health", wh_health)
    monkeypatch.setattr(vectors.VectorStore, "__init__", lambda self, path=None: None)
    monkeypatch.setattr(vectors.VectorStore, "health", lambda self: f.vectors)
    monkeypatch.setattr(jobs, "worker_alive", lambda: f.alive)

    def swarm(**kw: Any) -> dict[str, str]:
        return f.swarm(**kw)

    monkeypatch.setattr(lifecycle, "swarm_health", swarm)
    monkeypatch.setitem(sys.modules, "weasyprint", types.ModuleType("weasyprint"))
    monkeypatch.setattr(render, "health", lambda: f.templates)
    monkeypatch.setattr(doctor_host, "doctor_checks", host)
    return f


def _opts(env: CliEnv, **kw: Any) -> cli.GlobalOptions:
    return cli.GlobalOptions(config_dir=env.config_dir, **kw)


def _rows(env: CliEnv, *, sources: bool = False) -> dict[str, list[DoctorCheckResult]]:
    found: dict[str, list[DoctorCheckResult]] = {}
    for row in run_doctor(_opts(env), sources=sources):
        found.setdefault(row.name, []).append(row)
    return found


def _one(rows: dict[str, list[DoctorCheckResult]], name: str) -> DoctorCheckResult:
    assert len(rows[name]) == 1, rows[name]
    return rows[name][0]


def _main(args: list[str]) -> int:
    with pytest.raises(SystemExit) as exc:
        cli.main(args)
    assert isinstance(exc.value.code, int)
    return exc.value.code


# --- UT09-76 ------------------------------------------------------------------------------


def test_ut09_76_healthy_rows_in_table_order(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-76 a healthy system: every owned row PASS in the U09-94 order, then the spec 10
    rows in their order (R-07); the host checks see the CLI `--config-dir`."""
    rows = run_doctor(_opts(cli_env))
    names = [row.name for row in rows]
    assert names == [
        "config", "current_build", "ops_migrations", "startup_validation", "ops_store",
        "warehouse", "vectors", "worker", "swarm", "weasyprint", "reports_templates",
        "python_uv", "disk_free",
    ]  # fmt: skip
    assert all(row.status == "PASS" for row in rows[:11])
    assert rows[1].detail == "build `b1`, age 2.0 h"
    assert rows[-1] == DoctorCheckResult("disk_free", "WARN", "40 GB free", "Free space")
    assert fakes.host_config_dirs == [cli_env.config_dir]
    assert doctor_host._config_dir() == Path("config")  # the seam is restored
    assert fakes.validation_calls == [
        {"raise_on_error": False, "offline": True, "config_dir": cli_env.config_dir}
    ]
    stale = fakes.warehouse_calls[0]
    assert stale["stale_after_h"] == doctor.DOCTOR_BUILD_AGE_WARN_H == 48
    assert isinstance(stale["now"], datetime)
    assert stale["now"].utcoffset() is not None


def test_ut09_76_warn_and_fail_rules(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-76 missing CURRENT, pending migrations, no worker, no weasyprint with pdf and a
    start-up hook `error` issue: WARN/FAIL per rule with their fixes."""
    fakes.build = None
    fakes.pending = ["0007_jobs"]
    fakes.alive = False
    fakes.issues = [
        ConfigIssue("error", "metrics", "catalog invalid", "metrics.yaml"),
        ConfigIssue("warn", "enrich", "decider unused", "decisions.yaml"),
    ]
    sys.modules["weasyprint"] = None  # type: ignore[assignment] # import raises ImportError
    app_yaml = cli_env.config_dir / "app.yaml"
    app_yaml.write_text("version: 1\nreports: {formats: [html, pdf]}\n", encoding="utf-8")
    rows = _rows(cli_env)
    assert _one(rows, "current_build") == DoctorCheckResult(
        "current_build", "WARN", "No promoted warehouse yet.", "Run `herness pipeline`."
    )
    migrations = _one(rows, "ops_migrations")
    assert (migrations.status, migrations.fix) == ("FAIL", "Run `herness init`.")
    assert "0007_jobs" in migrations.detail
    error, warn = rows["startup_validation"]
    assert (error.status, warn.status) == ("FAIL", "WARN")
    assert error.detail == "error metrics metrics.yaml: catalog invalid"
    assert error.fix == "Fix the named file, then run `herness config validate`."
    assert _one(rows, "worker") == DoctorCheckResult(
        "worker", "WARN", "No worker is running.",
        "Start `herness worker` or the `herness-worker` task.",
    )  # fmt: skip
    pdf = _one(rows, "weasyprint")
    assert (pdf.status, pdf.fix) == ("FAIL", "`uv sync --extra pdf`")


def test_ut09_76_old_build_warns_and_no_pdf_engine_only_warns(
    fakes: Fakes, cli_env: CliEnv
) -> None:
    """UT09-76 a build older than 48 h is WARN; weasyprint missing without `pdf` is WARN;
    report templates `down` is FAIL."""
    fakes.build = _status.BuildView("b2", 49.25)
    fakes.templates = ("down", "templates failed to load: report.html.j2")
    sys.modules["weasyprint"] = None  # type: ignore[assignment]
    rows = _rows(cli_env)
    build = _one(rows, "current_build")
    assert (build.status, build.detail) == ("WARN", "build `b2`, age 49.2 h")
    assert build.fix == "Run `herness pipeline`."
    assert _one(rows, "weasyprint").status == "WARN"
    templates = _one(rows, "reports_templates")
    assert (templates.status, templates.detail) == ("FAIL", fakes.templates[1])


def test_ut09_76_unknown_build_age_warns(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-76 CURRENT readable but its build has no finished time: WARN, age unknown."""
    fakes.build = _status.BuildView("b3", None)
    build = _one(_rows(cli_env), "current_build")
    assert (build.status, build.detail) == ("WARN", "build `b3`, age unknown")


def test_ut09_76_exceptions_become_fail_with_class_name(
    fakes: Fakes, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-76 a check that raises is one FAIL row naming the error class; later checks
    still run."""

    def boom() -> tuple[str, str]:
        msg = "secret-looking text must not show"
        raise RuntimeError(msg)

    monkeypatch.setattr(ops, "ops_health", boom)
    monkeypatch.setattr(doctor_host, "doctor_checks", lambda cfg: 1 / 0)
    rows = _rows(cli_env)
    ops_row = _one(rows, "ops_store")
    assert (ops_row.status, ops_row.detail) == ("FAIL", "RuntimeError")
    assert "secret" not in ops_row.fix
    assert _one(rows, "host").detail == "ZeroDivisionError"
    assert _one(rows, "warehouse").status == "PASS"


def test_ut09_76_invalid_config_skips_later_checks(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-76 config load fails: `config` FAIL with the fix; every later check is WARN
    "skipped: config invalid" and no owner is called."""
    (cli_env.config_dir / "herness.yaml").write_text("version: 1\nbogus: 1\n", encoding="utf-8")
    rows = run_doctor(_opts(cli_env), sources=True)
    assert rows[0].name == "config"
    assert (rows[0].status, rows[0].fix) == ("FAIL", "Run `herness config validate`.")
    assert rows[0].detail
    assert {row.status for row in rows[1:]} == {"WARN"}
    assert {row.detail for row in rows[1:]} == {"skipped: config invalid"}
    assert [row.name for row in rows[-2:]] == ["sources", "host"]
    assert fakes.validation_calls == []
    assert fakes.host_config_dirs == []


def test_ut09_76_config_unexpected_error_is_fail(
    fakes: Fakes, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-76 a non-config exception while loading is FAIL with its class name."""
    del fakes

    def broken(self: cli.GlobalOptions, *, startup_validation: bool = True) -> None:
        del self, startup_validation
        raise OSError

    monkeypatch.setattr(cli.GlobalOptions, "config", broken)
    rows = run_doctor(_opts(cli_env))
    assert (rows[0].status, rows[0].detail) == ("FAIL", "OSError")


def test_ut09_76_doctor_exit_codes_and_json(
    fakes: Fakes, cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-76 `herness --json doctor`: cli/1 envelope with every row and its fix; exit 0
    with WARN rows only, exit 1 on any FAIL (R-46)."""
    cfg = str(cli_env.config_dir)
    assert _main(["--config-dir", cfg, "--json", "doctor"]) == 0
    envelope = json.loads(capsys.readouterr().out)
    assert set(envelope) == {"ok", "command", "data", "warnings", "error"}
    assert (envelope["ok"], envelope["command"], envelope["error"]) == (True, "doctor", None)
    assert envelope["data"]["schema"] == "cli/1"
    checks = envelope["data"]["checks"]
    assert checks[-1] == {"name": "disk_free", "status": "WARN", "detail": "40 GB free",
                          "fix": "Free space"}  # fmt: skip
    fakes.pending = ["0009_x"]
    assert _main(["--config-dir", cfg, "--json", "doctor"]) == 1
    envelope = json.loads(capsys.readouterr().out)
    assert (envelope["ok"], envelope["error"]) == (False, None)
    assert envelope["data"]["summary"] == {"PASS": 11, "WARN": 1, "FAIL": 1}


def test_ut09_76_doctor_human_table_and_fix_hints(
    fakes: Fakes, cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-76 the human table shows status, name and detail; fixes only with --fix-hints."""
    fakes.alive = False
    cfg = str(cli_env.config_dir)
    assert _main(["--config-dir", cfg, "doctor"]) == 0
    out = capsys.readouterr().out
    assert "WARN  worker" in out
    assert "No worker is running." in out
    assert "Fix:" not in out
    assert _main(["--config-dir", cfg, "doctor", "--fix-hints"]) == 0
    out = capsys.readouterr().out
    assert "Fix: Start `herness worker` or the `herness-worker` task." in out


def test_ut09_76_denied_user_runs_doctor(
    fakes: Fakes, cli_env: CliEnv, capsys: pytest.CaptureFixture[str]
) -> None:
    """UT09-76 doctor is in DENIED_ALLOWED: a denied OS user still gets the rows."""
    del fakes
    path = cli_env.config_dir / "herness.yaml"
    text = path.read_text(encoding="utf-8").replace("admins: [alice]", "admins: [root]")
    path.write_text(text.replace("default_role: viewer", "default_role: denied"), "utf-8")
    assert _main(["--config-dir", str(cli_env.config_dir), "--json", "doctor"]) == 0
    assert json.loads(capsys.readouterr().out)["data"]["checks"][0]["name"] == "config"


# --- UT09-107 -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("answer", "expected"),
    [
        ({"status": "ok", "reason": ""}, ("PASS", "", "")),
        (
            {"status": "degraded", "reason": "worker down"},
            (
                "WARN",
                "worker down",
                "Start `herness worker`, or resume or cancel the stuck run with `herness jobs`.",
            ),
        ),
        (
            {"status": "down", "reason": "StoreBusy"},
            (
                "FAIL",
                "StoreBusy",
                "Run `herness doctor` again; if it persists, check `data/ops.sqlite`.",
            ),
        ),
    ],
)
def test_ut09_107_swarm_row_per_health(
    fakes: Fakes, cli_env: CliEnv, answer: dict[str, str], expected: tuple[str, str, str]
) -> None:
    """UT09-107 `swarm_health` ok/degraded/down -> PASS, WARN with the reason and fix, FAIL
    with the reason; called with the `list_jobs`, `worker_alive` and `now` keywords."""
    calls: list[dict[str, Any]] = []

    def health(**kw: Any) -> dict[str, str]:
        calls.append(kw)
        return answer

    fakes.swarm = health
    row = _one(_rows(cli_env), "swarm")
    assert (row.status, row.detail, row.fix) == expected
    (call,) = calls
    assert set(call) == {"list_jobs", "worker_alive", "now"}
    assert call["list_jobs"] is jobs.list_jobs
    assert call["worker_alive"] is jobs.worker_alive
    assert isinstance(call["now"], datetime)


def test_ut09_107_swarm_raising_is_fail_with_class_name(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-107 `swarm_health` raising StoreBusy: FAIL with the error class name."""

    def health(**kw: Any) -> dict[str, str]:
        del kw
        msg = "locked"
        raise StoreBusy(msg)

    fakes.swarm = health
    row = _one(_rows(cli_env), "swarm")
    assert (row.status, row.detail) == ("FAIL", "StoreBusy")


# --- UT09-108 -----------------------------------------------------------------------------

_FIXES = {
    "ops_store": ("Run `herness init`.",
                  "Check that `data/ops.sqlite` exists and is not locked by another program."),
    "warehouse": ("Run `herness pipeline`.", "Run `herness pipeline`."),
    "vectors": ("Run `herness build` to create the vector tables.",
                "Check the `data/vectors` directory."),
}  # fmt: skip


@pytest.mark.parametrize("name", ["ops_store", "warehouse", "vectors"])
@pytest.mark.parametrize(
    ("health", "status"),
    [(("ok", ""), "PASS"), (("degraded", "r"), "WARN"), (("down", "StoreBusy"), "FAIL")],
)
def test_ut09_108_store_health_rows(
    fakes: Fakes, cli_env: CliEnv, name: str, health: tuple[str, str], status: str,
    capsys: pytest.CaptureFixture[str],
) -> None:  # fmt: skip
    """UT09-108 `ops_health`, `warehouse_health` and `VectorStore.health` ok/degraded/down ->
    PASS, WARN with reason `r` and fix, FAIL with reason `StoreBusy`; exit 1 only on FAIL."""
    attr = {"ops_store": "ops", "warehouse": "warehouse", "vectors": "vectors"}[name]
    setattr(fakes, attr, health)
    row = _one(_rows(cli_env), name)
    warn_fix, fail_fix = _FIXES[name]
    expected_fix = {"PASS": "", "WARN": warn_fix, "FAIL": fail_fix}[status]
    assert (row.status, row.detail, row.fix) == (status, health[1], expected_fix)
    code = _main(["--config-dir", str(cli_env.config_dir), "--json", "doctor"])
    capsys.readouterr()
    assert code == (1 if status == "FAIL" else 0)


# --- --sources ----------------------------------------------------------------------------


def test_ut09_76_sources_rows_through_registry(
    fakes: Fakes, cli_env: CliEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UT09-76 `--sources`: one row per enabled source from the registry-built connector's
    `.check()`; a failure is FAIL with the class name and its user fix; start-up validation
    runs online."""
    from herness.connectors import factory  # noqa: PLC0415 - patched where used
    from herness.core.errors import AuthError  # noqa: PLC0415

    class Conn:
        def __init__(self, name: str) -> None:
            self.name = name

        def check(self) -> None:
            if self.name == "jira":
                msg = "rejected"
                details = {"source": "jira", "secret": "jira.token"}  # pragma: allowlist secret
                raise AuthError(msg, details=details)

    enabled = [("servicenow", object()), ("jira", object())]
    monkeypatch.setattr(factory, "build_connector", lambda name, cfg: Conn(name))
    monkeypatch.setattr(SourcesConfig, "enabled_sources", lambda self: enabled)
    rows = _rows(cli_env, sources=True)
    ok, failed = rows["sources"]
    assert (ok.status, ok.detail) == ("PASS", "servicenow: check passed")
    assert (failed.status, failed.detail) == ("FAIL", "jira: AuthError")
    assert failed.fix == "`herness secrets set jira.token`; `herness doctor --sources`."
    assert fakes.validation_calls[0]["offline"] is False


def test_ut09_76_sources_none_enabled(fakes: Fakes, cli_env: CliEnv) -> None:
    """UT09-76 `--sources` with no enabled source: one PASS row saying so."""
    del fakes
    row = _one(_rows(cli_env, sources=True), "sources")
    assert (row.status, row.detail) == ("PASS", "no source is enabled")
