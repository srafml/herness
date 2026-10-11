"""System commands: ``init``, ``doctor``, ``status``, ``ui``, ``worker``, ``gpu load|unload``
(impl 09 U09-93, U09-94).

``init``'s steps live in ``_init`` and the ``status`` reads in ``_status`` (budget split).
``ui`` starts the checkout's ``app/Home.py`` (T09-13; not wheel package data) through the
module-level ``_runner`` (tests replace it). Owner modules are imported inside the command
bodies (BT09-07).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, cast

import typer
from rich.console import Console

from herness._cli._job_cmds import current_opts
from herness._cli.identity import guarded
from herness._cli.output import CliResult, emit, markup, table_printer
from herness.core import time as clock
from herness.core.errors import ConfigError, JobStateError
from herness.reports.rules import Actor, UserInputError  # Actor: Typer reads the hints

if TYPE_CHECKING:
    from herness.cli import GlobalOptions
    from herness.core.config import HernessConfig
    from herness.core.types import GpuClass

__all__ = ["APP_HOME", "GPU_LOAD_CLASSES", "register", "ui_args"]

_REPO_DIR: Final = Path(__file__).resolve().parents[2]
APP_HOME: Final = _REPO_DIR / "app" / "Home.py"  # the T09-13 app entry page
_LOOPBACK: Final = frozenset({"127.0.0.1", "::1", "localhost"})
_GPU_CLASSES: Final = ("none", "reasoning", "decider", "large")
GPU_LOAD_CLASSES: Final = ("reasoning", "decider", "large")
_BOOTSTRAP: Final = "herness.cli:worker_bootstrap"
_NEXT: Final = "Next: run `herness secrets init`."
_runner: Callable[..., subprocess.CompletedProcess[bytes]] = subprocess.run
_Opt = typer.Option


# --- init ----------------------------------------------------------------------------------------


def _init_printer(data: dict[str, object]) -> Callable[[Console], None]:
    table = table_printer(data)

    def show(console: Console) -> None:
        table(console)
        console.print(markup(_NEXT))

    return show


@guarded("init")
def init(
    *, force: Annotated[bool, _Opt("--force", help="Back up and replace config files.")] = False,
    actor: Actor | None,
) -> int:  # fmt: skip
    """Create the data folders, copy the config templates and migrate the ops store."""
    from herness._cli._init import run_init  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    opts = current_opts()
    data = run_init(opts, force=force)
    return emit(opts, CliResult(opts.command, data, [], _init_printer(data)))


# --- doctor, status ------------------------------------------------------------------------------


def _doctor_printer(rows: list[dict[str, str]], *, fixes: bool) -> Callable[[Console], None]:
    def show(console: Console) -> None:
        for row in rows:
            console.print(markup(f"{row['status']:<4}  {row['name']:<20}  {row['detail']}"))
            if fixes and row["fix"]:
                console.print(markup(f"      Fix: {row['fix']}"))

    return show


@guarded("doctor")
def doctor(
    *, fix_hints: Annotated[bool, _Opt("--fix-hints", help="Show the fix of each row.")] = False,
    sources: Annotated[bool, _Opt("--sources", help="Also check every enabled source.")] = False,
    actor: Actor | None,
) -> int:  # fmt: skip
    """Check the installation; exit 1 when any row is FAIL."""
    from herness._cli.doctor import run_doctor  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    opts = current_opts()
    rows = [vars(row) for row in run_doctor(opts, sources=sources)]
    summary = {status: sum(r["status"] == status for r in rows) for status in ("PASS", "WARN",
                                                                               "FAIL")}  # fmt: skip
    data: dict[str, object] = {"checks": rows, "summary": summary}
    code = 1 if summary["FAIL"] else 0
    return emit(opts, CliResult(opts.command, data, [], _doctor_printer(rows, fixes=fix_hints),
                                code))  # fmt: skip


def _status_printer(data: dict[str, object]) -> Callable[[Console], None]:
    table = table_printer({k: v for k, v in data.items() if k != "current_build"})

    def show(console: Console) -> None:
        from herness._cli._status import NO_CURRENT  # noqa: PLC0415 - lazy (BT09-07)

        build = cast("dict[str, object] | None", data["current_build"])
        if build is None:
            console.print(markup(NO_CURRENT))
        else:
            age = "unknown" if build["age_h"] is None else f"{build['age_h']} h"
            console.print(markup(f"Current build: {build['build_id']}, age {age}"))
        table(console)

    return show


@guarded("status")
def status(*, actor: Actor | None) -> int:
    """Show the current build, jobs, workers, recent runs and the chat mode."""
    from herness._cli._status import collect_status  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    opts = current_opts()
    data, warnings = collect_status(opts.config(), clock.now())
    return emit(opts, CliResult(opts.command, data, warnings, _status_printer(data)))


# --- ui, worker ----------------------------------------------------------------------------------


def _child_env(opts: GlobalOptions, cfg: HernessConfig) -> dict[str, str]:
    """The variables a child process needs to load the same config (U09-93, U09-104)."""
    return {
        "HERNESS_CONFIG_DIR": str(opts.config_dir.resolve()),
        "HERNESS_DATA_DIR": str(cfg.paths.data),
        "HERNESS_PROFILE": str(cfg.profile),
        "HERNESS_SET_OVERRIDES": json.dumps(list(opts.set_overrides)),
    }


def ui_args(bind: str, port: int) -> list[str]:
    """The Streamlit argument list; ConfigError for a non-loopback bind (TH09-05, R-50)."""
    if bind not in _LOOPBACK:
        msg = "dashboard bind must be loopback"
        raise ConfigError(msg, hint="Set `security.ui.bind` to 127.0.0.1 (design 09 §9.1).")
    return [
        sys.executable, "-m", "streamlit", "run", str(APP_HOME), "--server.address", bind,
        "--server.port", str(port), "--server.headless", "true",
        "--browser.gatherUsageStats", "false", "--server.enableXsrfProtection", "true",
        "--server.enableCORS", "true",
    ]  # fmt: skip


@guarded("ui")
def ui(
    *, port: Annotated[int | None, _Opt("--port", min=1024, max=65535, help="Port.")] = None,
    actor: Actor | None,
) -> int:  # fmt: skip
    """Start the dashboard on loopback; returns the server's exit code."""
    del actor
    opts = current_opts()
    cfg = opts.config()
    args = ui_args(cfg.security.ui.bind, cfg.security.ui.port if port is None else port)
    env = {**os.environ, **_child_env(opts, cfg), "HF_HUB_OFFLINE": "1", "DO_NOT_TRACK": "1"}
    return int(_runner(args, env=env, check=False).returncode)  # the server's lifetime


def _gpu_classes(text: str | None) -> list[GpuClass]:
    if text is None:
        return list(cast("tuple[GpuClass, ...]", _GPU_CLASSES))
    names = [part.strip() for part in text.split(",")]
    if not all(name in _GPU_CLASSES for name in names):
        msg = "--gpu-class takes a comma-separated subset of none,reasoning,decider,large"
        raise UserInputError(msg, hint="Run `herness worker --help`.")
    return cast("list[GpuClass]", list(dict.fromkeys(names)))


@guarded("worker")
def worker(
    *, gpu_class: Annotated[str | None, _Opt("--gpu-class", help="none,reasoning,decider,large "
                                                               "subset.")] = None,
    concurrency: Annotated[int | None, _Opt("--concurrency", min=1, max=64, help="CPU slots.")]
    = None,
    once: Annotated[bool, _Opt("--once", help="Run at most one job, then exit.")] = False,
    actor: Actor | None,
) -> int:  # fmt: skip
    """Run the job worker in this console (spec 08)."""
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)

    del actor
    opts = current_opts()
    classes = _gpu_classes(gpu_class)
    os.environ.update(_child_env(opts, opts.config()))  # inherited by the job children
    return jobs.run_worker(gpu_classes=classes, concurrency=concurrency, once=once,
                           bootstrap=_BOOTSTRAP)  # fmt: skip


# --- gpu -----------------------------------------------------------------------------------------


def _request(cls: GpuClass) -> int:
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)

    opts = current_opts()
    opts.config()
    if jobs.request_gpu_class(cls) == "no_worker":
        msg = "No worker is running."
        hint = "Start `herness worker`, or use `herness deploy up CLASS`."
        raise JobStateError(msg, hint=hint, details={"code": "no_worker"})
    data: dict[str, object] = {"requested_class": cls}
    return emit(opts, CliResult(opts.command, data, [], table_printer(data)))


@guarded("gpu load")
def gpu_load(
    cls: Annotated[str, typer.Argument(metavar="CLASS", help="reasoning, decider or large.")],
    *, actor: Actor | None,
) -> int:  # fmt: skip
    """Ask the running worker to load a GPU class (design 08 §5.8)."""
    del actor
    if cls not in GPU_LOAD_CLASSES:
        msg = "CLASS must be reasoning, decider or large"
        raise UserInputError(msg, hint="Run `herness gpu load --help`.")
    return _request(cast("GpuClass", cls))


@guarded("gpu unload")
def gpu_unload(*, actor: Actor | None) -> int:
    """Ask the running worker to unload the GPU class."""
    del actor
    return _request("none")


def register(root: typer.Typer) -> None:
    """Register the system commands on the root app (U09-84 step 4)."""
    for name, handler in (("init", init), ("doctor", doctor), ("status", status), ("ui", ui),
                          ("worker", worker)):  # fmt: skip
        root.command(name)(handler)
    gpu = typer.Typer(name="gpu", no_args_is_help=True, help="Manual GPU class switch.")
    gpu.command("load")(gpu_load)
    gpu.command("unload")(gpu_unload)
    root.add_typer(gpu, name="gpu")
