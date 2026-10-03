"""Typer root, entry point and error boundary of the ``herness`` command (impl 09 U09-84,
U09-85, U09-106).

``main`` installs the socket guard before any command code runs, maps every outcome to an R-46
exit code and keeps stdout for command output only. Heavy and upper-layer modules are imported
inside functions so ``herness --help`` stays fast (BT09-07).
"""

from __future__ import annotations

import importlib.metadata
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Final, NoReturn, cast

import typer
from typer._click.exceptions import UsageError

from herness._cli.identity import cli_actor
from herness._cli.output import EXIT_CODES, emit_error, exit_code_for
from herness.core import time as clock
from herness.core.config_sources import BootstrapConfig, load_bootstrap, resolve_profile
from herness.core.egress_socket import install_socket_guard
from herness.core.errors import ConfigError, HernessError
from herness.core.logging import configure_logging, get_logger
from herness.core.resilience.metrics import flush_metrics, record_counter
from herness.core.settings import SecurityConfig
from herness.reports.rules import UserInputError

if TYPE_CHECKING:
    from herness.core.config import HernessConfig
    from herness.core.config_sources import ProfileName
    from herness.core.config_view import ConfigIssue
    from herness.reports.rules import Actor

__all__ = ["GlobalOptions", "app", "main", "run_startup_validation"]

_log = get_logger("cli")
_SET_RE: Final = re.compile(r"^[a-z0-9_]+(\.[a-z0-9_]+)+=.*$")
_MAX_METRICS_BYTES: Final = 1_048_576
_METRICS_ISSUE: Final = "metric catalog file is missing, too large or invalid"
_CONFIG_DIR: Final = Path("config")

app = typer.Typer(
    name="herness", no_args_is_help=True, add_completion=False, pretty_exceptions_enable=False
)


@dataclass
class GlobalOptions:
    """Global options shared by every command through ``ctx.obj`` (U09-85)."""

    config_dir: Path = Path("config")
    data_dir: Path | None = None
    profile: str | None = None
    set_overrides: tuple[str, ...] = ()
    json: bool = False
    quiet: bool = False
    verbose: bool = False
    worker_warned: bool = False
    command: str = "herness"  # set by `guarded` to the running command path
    _cfg: HernessConfig | None = field(default=None, init=False, repr=False)
    _validated: bool = field(default=False, init=False, repr=False)

    def config(self, *, startup_validation: bool = True) -> HernessConfig:
        """Load the config once (logging, port binding), then run start-up validation once."""
        if self._cfg is None:
            from herness.core.config import init_config  # noqa: PLC0415 - lazy (BT09-07)
            from herness.core.secrets import scrub_secrets  # noqa: PLC0415
            from herness.store.ops.resilience import bind_core_backends  # noqa: PLC0415

            overrides = self.set_overrides
            if self.data_dir is not None:
                overrides += (f"paths.data={self.data_dir.as_posix()}",)
            profile = cast("ProfileName | None", self.profile)  # the loader validates the name
            cfg = init_config(profile, overrides, self.config_dir)
            level = "DEBUG" if self.verbose else cfg.logging.level
            configure_logging(level, log_dir=cfg.paths.logs, scrubber=scrub_secrets, stderr=True)
            bind_core_backends()  # R-04
            self._cfg = cfg
        if startup_validation and not self._validated:
            run_startup_validation(self._cfg, config_dir=self.config_dir)
            self._validated = True
        return self._cfg

    def actor(self, *, need_ref: bool) -> Actor:
        """The CLI actor of this process (U09-89 ``cli_actor``)."""
        return cli_actor(self.config(), need_ref=need_ref)


# --- U09-106 start-up validation ----------------------------------------------------------------


class _MetricsCatalogValidator:
    """Owner validator ``metrics.catalog``: ``<config_dir>/metrics.yaml`` -> ``validate_catalog``.

    One module-level instance, so a repeated registration is the same object (a no-op).
    """

    def __init__(self) -> None:
        self.config_dir = _CONFIG_DIR

    def __call__(self, cfg: HernessConfig, *, offline: bool) -> list[ConfigIssue]:
        import yaml  # noqa: PLC0415 - lazy (BT09-07)
        from pydantic import ValidationError  # noqa: PLC0415

        from herness.core.config_view import ConfigIssue  # noqa: PLC0415
        from herness.metrics.catalog import validate_catalog  # noqa: PLC0415
        from herness.metrics.settings import MetricsCatalogConfig  # noqa: PLC0415

        del offline  # the check reads one local file
        path = self.config_dir / "metrics.yaml"
        catalog = None
        try:
            if path.stat().st_size <= _MAX_METRICS_BYTES:
                raw = yaml.safe_load(path.read_text(encoding="utf-8"))
                catalog = MetricsCatalogConfig.model_validate(raw)
        except (OSError, UnicodeDecodeError, yaml.YAMLError, ValidationError):
            catalog = None
        if catalog is None:
            return [ConfigIssue("error", "metrics", _METRICS_ISSUE, "metrics.yaml")]
        return validate_catalog(catalog, weights=cfg.weights)


_METRICS_CATALOG: Final = _MetricsCatalogValidator()


def _enrich_deciders(cfg: HernessConfig, *, offline: bool) -> list[dict[str, str]]:
    """Owner validator ``enrich.deciders`` (impl 03 U03-151, R-71)."""
    from herness.enrich.settings import check_decider_refs  # noqa: PLC0415 - lazy (BT09-07)

    del offline  # the check reads the loaded config only
    return check_decider_refs(cfg.decisions, cfg.models.deciders)


def run_startup_validation(
    cfg: HernessConfig,
    *,
    raise_on_error: bool = True,
    offline: bool = True,
    config_dir: Path = _CONFIG_DIR,
) -> list[ConfigIssue]:
    """Register the owner validators (once) and run them (U09-106, R-71)."""
    from herness.core import config_validate  # noqa: PLC0415 - lazy (BT09-07)
    from herness.core.jobs.validate import validate_resilience_config  # noqa: PLC0415

    _METRICS_CATALOG.config_dir = config_dir
    config_validate.register_owner_validator("metrics.catalog", _METRICS_CATALOG)
    config_validate.register_owner_validator("resilience", validate_resilience_config)
    config_validate.register_owner_validator("enrich.deciders", _enrich_deciders)
    issues = config_validate.run_owner_validators(cfg, offline=offline)
    errors = [issue for issue in issues if issue.severity == "error"]
    _log.info("config.startup.validated", errors=len(errors), warnings=len(issues) - len(errors))
    if raise_on_error and errors:
        details = {"issues": "; ".join(str(issue) for issue in errors)[:2000]}
        msg = f"start-up validation failed: {len(errors)} errors"
        raise ConfigError(msg, details=details)
    return issues


# --- U09-85 root callback ------------------------------------------------------------------------


def _version(value: bool) -> None:
    if value:
        sys.stdout.write(f"herness {importlib.metadata.version('herness')}\n")
        raise typer.Exit(0)


def _check_sets(values: list[str] | None) -> list[str] | None:
    for value in values or ():
        if not _SET_RE.fullmatch(value):
            msg = "expected a.b.c=<yaml> with lowercase key segments"
            raise typer.BadParameter(msg, param_hint="--set")
    return values


_EPILOG: Final = "Exit codes: " + "; ".join(f"{c} {m}" for c, m in EXIT_CODES.items())
_Opt = typer.Option


@app.callback(epilog=_EPILOG)
def _root(  # noqa: PLR0913 - one parameter per global option (U09-85)
    ctx: typer.Context,
    *,
    config_dir: Annotated[Path, _Opt("--config-dir", help="Config directory.")] = _CONFIG_DIR,
    data_dir: Annotated[Path | None, _Opt("--data-dir", help="Data directory.")] = None,
    profile: Annotated[str | None, _Opt("--profile", help="Profile (spec 10).")] = None,
    set_overrides: Annotated[
        list[str] | None,
        _Opt("--set", help="Override a.b.c=<yaml> (repeatable).", callback=_check_sets),
    ] = None,
    json_mode: Annotated[bool, _Opt("--json", help="JSON envelope on stdout.")] = False,
    quiet: Annotated[bool, _Opt("--quiet", help="No progress or table headers.")] = False,
    verbose: Annotated[bool, _Opt("--verbose", help="DEBUG logs and tracebacks.")] = False,
    version: Annotated[
        bool, _Opt("--version", is_eager=True, callback=_version, help="Print the version.")
    ] = False,
) -> None:
    """Herness command line (design 09 §5.6)."""
    del version
    if quiet and verbose:
        msg = "--quiet and --verbose cannot be used together"
        raise typer.BadParameter(msg)
    opts = ctx.ensure_object(GlobalOptions)
    opts.config_dir, opts.data_dir, opts.profile = config_dir, data_dir, profile
    opts.set_overrides = tuple(set_overrides or ())
    opts.json, opts.quiet, opts.verbose = json_mode, quiet, verbose


# --- U09-84 entry point --------------------------------------------------------------------------

_registered = False


def _register_groups(root: typer.Typer) -> None:
    """U09-84 step 4: each command module registers its group on the root app."""
    # T09-22: cmd_system.register(root)
    # T09-23: cmd_data.register(root); cmd_review.register(root)
    # T09-24: cmd_queue.register(root); cmd_admin.register(root)
    # T09-25: cmd_chat.register(root)
    del root


def _prescan(args: Sequence[str], name: str) -> str | None:
    """Value of the first ``name`` option in ``args`` (``--x=v`` or ``--x v``)."""
    for index, arg in enumerate(args):
        if arg.startswith(name + "="):
            return arg.partition("=")[2]
        if arg == name:
            return args[index + 1] if index + 1 < len(args) else None
    return None


def _bootstrap(config_dir: Path, profile: str | None) -> BootstrapConfig:
    """Bootstrap config for the socket guard; secure default without ``herness.yaml``."""
    if (config_dir / "herness.yaml").is_file():
        return load_bootstrap(cast("ProfileName | None", profile), config_dir)
    return BootstrapConfig(resolve_profile(profile, os.environ), SecurityConfig(), ())


def _run(opts: GlobalOptions, args: list[str]) -> int:
    global _registered  # noqa: PLW0603 - command groups are registered once per process
    try:
        configure_logging("DEBUG" if opts.verbose else "INFO", stderr=True)  # stdout stays clean
        config_dir = Path(_prescan(args, "--config-dir") or "config")
        install_socket_guard(_bootstrap(config_dir, _prescan(args, "--profile")))
        if not _registered:
            _register_groups(app)
            _registered = True
        result = app(args=args, standalone_mode=False, obj=opts)
        return result if isinstance(result, int) else 0
    except (UsageError, UserInputError) as exc:
        if isinstance(exc, UsageError) and not opts.json:
            exc.show()
            return 2
        return emit_error(opts, opts.command, exc)
    except KeyboardInterrupt:
        return exit_code_for(KeyboardInterrupt())
    except HernessError as exc:
        return emit_error(opts, opts.command, exc)
    except Exception as exc:  # noqa: BLE001 - the CLI error boundary (ENG §3.4)
        _log.error("cli.command.failed", command=opts.command, error_type=type(exc).__name__)
        return emit_error(opts, opts.command, exc)


def main(argv: Sequence[str] | None = None) -> NoReturn:
    """Run the CLI with ``argv`` (default ``sys.argv[1:]``) and exit with the R-46 code."""
    args = list(sys.argv[1:] if argv is None else argv)
    opts = GlobalOptions(json="--json" in args, verbose="--verbose" in args)
    started = clock.monotonic()
    code = _run(opts, args)
    duration_ms = int((clock.monotonic() - started) * 1000)
    _log.info(
        "cli.command.completed", command=opts.command, exit_code=code, duration_ms=duration_ms
    )
    labels = {"command": opts.command.replace(" ", "."), "exit_code": str(code)}
    record_counter("herness_cli_commands_total", component="cli", labels=labels)
    if opts._cfg is not None:
        flush_metrics()  # a short-lived process: write the counter before exit
    sys.exit(code)
