"""The doctor rows that call outside the stores (impl 09 U09-94): ``startup_validation``
(U09-106 hook), ``sources`` (``--sources``, connectors from the registry) and the spec 10 host
rows (R-07); private sibling of ``doctor`` (T09-22, budget split).

``doctor`` wraps every function here, so an exception becomes one FAIL row.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import TYPE_CHECKING, Final

if TYPE_CHECKING:
    from pathlib import Path

    from herness._cli.doctor import DoctorCheckResult, _Ctx

__all__ = ["host", "sources", "startup_validation"]

_ISSUE_FIX: Final = "Fix the named file, then run `herness config validate`."


def startup_validation(ctx: _Ctx) -> list[DoctorCheckResult]:
    """Each start-up hook issue as a row: ``error`` FAIL, ``warn`` WARN; none PASS (R-71)."""
    from herness import cli  # noqa: PLC0415 - the composition root owns the hook (U09-106)
    from herness._cli.doctor import DoctorCheckResult as Row  # noqa: PLC0415 - import cycle

    issues = cli.run_startup_validation(ctx.cfg, raise_on_error=False, offline=not ctx.sources,
                                        config_dir=ctx.opts.config_dir)  # fmt: skip
    rows = [Row("startup_validation", "FAIL" if i.severity == "error" else "WARN", str(i),
                _ISSUE_FIX) for i in issues]  # fmt: skip
    return rows or [Row("startup_validation", "PASS", "no issues", "")]


def sources(ctx: _Ctx) -> list[DoctorCheckResult]:
    """One row per enabled source: the registry-built connector's ``check()``."""
    from herness._cli.doctor import DoctorCheckResult as Row  # noqa: PLC0415 - import cycle
    from herness.connectors import factory  # noqa: PLC0415 - lazy: registry.get inside
    from herness.reports.rules import user_message  # noqa: PLC0415

    rows: list[Row] = []
    for name, _ in ctx.cfg.sources.enabled_sources():
        try:
            factory.build_connector(name, ctx.cfg).check()
        except Exception as exc:  # noqa: BLE001 - one FAIL row per source (U09-94)
            rows.append(Row("sources", "FAIL", f"{name}: {type(exc).__name__}",
                            user_message(exc)[1]))  # fmt: skip
        else:
            rows.append(Row("sources", "PASS", f"{name}: check passed", ""))
    return rows or [Row("sources", "PASS", "no source is enabled", "")]


@contextlib.contextmanager
def _config_dir_seam(config_dir: Path) -> Iterator[None]:
    """Point impl 10's ``_config_dir`` seam at the CLI ``--config-dir`` (T10-27 carry-over),
    so the ``.env`` and profile targets are not read relative to the working directory."""
    from herness.admin import doctor_host  # noqa: PLC0415 - lazy (BT09-07)

    saved = doctor_host._config_dir
    doctor_host._config_dir = lambda: config_dir
    try:
        yield
    finally:
        doctor_host._config_dir = saved


def host(ctx: _Ctx) -> list[DoctorCheckResult]:
    """The design 10 §5.6.3 rows of ``doctor_checks`` in their order (R-07)."""
    from herness._cli.doctor import DoctorCheckResult as Row  # noqa: PLC0415 - import cycle
    from herness.admin import doctor_host  # noqa: PLC0415 - lazy (BT09-07)

    with _config_dir_seam(ctx.opts.config_dir):
        rows = doctor_host.doctor_checks(ctx.cfg)
    return [Row(r.name, r.status, r.detail, r.fix) for r in rows]
