"""``herness doctor`` checks (impl 09 U09-94): the rows owned here, then the spec 10 host rows.

Every check is wrapped: an exception becomes one FAIL row naming the error class, so
``run_doctor`` never raises. With an invalid config the later checks are WARN rows
"skipped: config invalid". Details name ids, paths and reasons, never a secret value (spec 10
rows report presence only). Owner modules are imported inside the checks (BT09-07).
"""

from __future__ import annotations

import importlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Final, Literal

from herness._cli import _doctor_ext, _status
from herness.core import time as clock
from herness.core.errors import ConfigError
from herness.core.logging import get_logger

if TYPE_CHECKING:
    from herness.cli import GlobalOptions
    from herness.core.config import HernessConfig

__all__ = ["DOCTOR_BUILD_AGE_WARN_H", "DoctorCheckResult", "run_doctor"]

type _Status = Literal["PASS", "WARN", "FAIL"]

DOCTOR_BUILD_AGE_WARN_H: Final = 48
_log = get_logger("cli")
_SKIPPED: Final = "skipped: config invalid"
_PIPELINE: Final = "Run `herness pipeline`."
_INIT: Final = "Run `herness init`."
_FIXES: Final[dict[str, tuple[str, str]]] = {  # name -> (WARN fix, FAIL fix)
    "ops_store": (_INIT,
                  "Check that `data/ops.sqlite` exists and is not locked by another program."),
    "warehouse": (_PIPELINE, _PIPELINE),
    "vectors": ("Run `herness build` to create the vector tables.",
                "Check the `data/vectors` directory."),
    "swarm": ("Start `herness worker`, or resume or cancel the stuck run with `herness jobs`.",
              "Run `herness doctor` again; if it persists, check `data/ops.sqlite`."),
    "reports_templates": ("`uv sync --extra pdf`", "Reinstall Herness: a report template "
                          "did not load."),
}  # fmt: skip
_CONFIG_FIX: Final = "Run `herness config validate`."
_WORKER_FIX: Final = "Start `herness worker` or the `herness-worker` task."
_PDF_FIX: Final = "`uv sync --extra pdf`"
_ERROR_FIX: Final = "See the Herness log for the error."


@dataclass(frozen=True)
class DoctorCheckResult:
    """One doctor row (U09-94); JSON ``{"name", "status", "detail", "fix"}``."""

    name: str
    status: _Status
    detail: str
    fix: str


@dataclass(frozen=True)
class _Ctx:
    opts: GlobalOptions
    cfg: HernessConfig
    sources: bool


def _health(name: str, status: str, reason: str) -> list[DoctorCheckResult]:
    """An owner's ``ok``/``degraded``/``down`` as PASS, WARN or FAIL with the row's fixes."""
    warn_fix, fail_fix = _FIXES[name]
    if status == "ok":
        return [DoctorCheckResult(name, "PASS", reason, "")]
    if status == "degraded":
        return [DoctorCheckResult(name, "WARN", reason, warn_fix)]
    return [DoctorCheckResult(name, "FAIL", reason, fail_fix)]


def _current_build(ctx: _Ctx) -> list[DoctorCheckResult]:
    del ctx
    build = _status.current_build(clock.now())
    if build is None:
        return [DoctorCheckResult("current_build", "WARN", _status.NO_CURRENT, _PIPELINE)]
    if build.age_h is None:
        return [DoctorCheckResult("current_build", "WARN", f"build `{build.build_id}`, age unknown",
                            _PIPELINE)]  # fmt: skip
    detail = f"build `{build.build_id}`, age {build.age_h:.1f} h"
    if build.age_h > DOCTOR_BUILD_AGE_WARN_H:
        return [DoctorCheckResult("current_build", "WARN", detail, _PIPELINE)]
    return [DoctorCheckResult("current_build", "PASS", detail, "")]


def _ops_migrations(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.store import ops  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    pending = ops.pending_migrations()
    if pending:
        return [
            DoctorCheckResult("ops_migrations", "FAIL", "pending: " + ", ".join(pending), _INIT)
        ]
    return [DoctorCheckResult("ops_migrations", "PASS", "none pending", "")]


def _ops_store(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.store import ops  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    return _health("ops_store", *ops.ops_health())


def _warehouse(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.store import warehouse  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    health = warehouse.warehouse_health(now=clock.now(), stale_after_h=DOCTOR_BUILD_AGE_WARN_H)
    return _health("warehouse", *health)


def _vectors(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.store.vectors import VectorStore  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    return _health("vectors", *VectorStore().health())


def _worker(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    if jobs.worker_alive():
        return [DoctorCheckResult("worker", "PASS", "a worker is running", "")]
    return [DoctorCheckResult("worker", "WARN", "No worker is running.", _WORKER_FIX)]


def _swarm(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.core import jobs  # noqa: PLC0415 - lazy (BT09-07)
    from herness.harness.swarm import lifecycle  # noqa: PLC0415

    del ctx
    health = lifecycle.swarm_health(list_jobs=jobs.list_jobs, worker_alive=jobs.worker_alive,
                                    now=clock.now())  # fmt: skip
    return _health("swarm", health["status"], health["reason"])


def _weasyprint(ctx: _Ctx) -> list[DoctorCheckResult]:
    try:
        importlib.import_module("weasyprint")
    except Exception:  # noqa: BLE001 - ImportError, or OSError when GTK/Pango is missing
        status: _Status = "FAIL" if "pdf" in ctx.cfg.app.reports.formats else "WARN"
        return [DoctorCheckResult("weasyprint", status, "not importable", _PDF_FIX)]
    return [DoctorCheckResult("weasyprint", "PASS", "importable", "")]


def _reports_templates(ctx: _Ctx) -> list[DoctorCheckResult]:
    from herness.reports import render  # noqa: PLC0415 - lazy (BT09-07)

    del ctx
    return _health("reports_templates", *render.health())


_CHECKS: Final[tuple[tuple[str, Callable[[_Ctx], list[DoctorCheckResult]]], ...]] = (
    ("current_build", _current_build), ("ops_migrations", _ops_migrations),
    ("startup_validation", _doctor_ext.startup_validation), ("ops_store", _ops_store),
    ("warehouse", _warehouse), ("vectors", _vectors), ("worker", _worker), ("swarm", _swarm),
    ("weasyprint", _weasyprint), ("reports_templates", _reports_templates),
)  # fmt: skip


def _run(
    name: str, check: Callable[[_Ctx], list[DoctorCheckResult]], ctx: _Ctx
) -> list[DoctorCheckResult]:
    try:
        return check(ctx)
    except Exception as exc:  # noqa: BLE001 - checks never raise (U09-94)
        _log.warning("cli.doctor.check_failed", check=name, error_type=type(exc).__name__)
        return [DoctorCheckResult(name, "FAIL", type(exc).__name__, _ERROR_FIX)]


def _config_row(opts: GlobalOptions) -> tuple[DoctorCheckResult, HernessConfig | None]:
    try:
        cfg = opts.config(startup_validation=False)
    except ConfigError as exc:
        return DoctorCheckResult("config", "FAIL", exc.message, _CONFIG_FIX), None
    except Exception as exc:  # noqa: BLE001 - checks never raise (U09-94)
        return DoctorCheckResult("config", "FAIL", type(exc).__name__, _CONFIG_FIX), None
    return DoctorCheckResult("config", "PASS", f"profile {cfg.profile}", ""), cfg


def run_doctor(opts: GlobalOptions, *, sources: bool = False) -> list[DoctorCheckResult]:
    """Every check in the U09-94 table order, then the spec 10 rows; never raises."""
    extra = (("sources", _doctor_ext.sources),) if sources else ()
    checks = [*_CHECKS, *extra, ("host", _doctor_ext.host)]
    first, cfg = _config_row(opts)
    if cfg is None:
        return [first, *(DoctorCheckResult(name, "WARN", _SKIPPED, "") for name, _ in checks)]
    ctx = _Ctx(opts, cfg, sources)
    return [first, *(row for name, check in checks for row in _run(name, check, ctx))]
