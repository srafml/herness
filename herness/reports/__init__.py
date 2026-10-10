"""Report outputs (impl 09): ``render_run``, loaded lazily (`herness.core.config` imports
`herness.reports.settings`), and ``ReportManifest`` (owner `herness.core.types`, R-01)."""

from typing import TYPE_CHECKING

from herness.core.types import ReportManifest

if TYPE_CHECKING:
    from herness.reports.render import render_run

__all__: tuple[str, ...] = ("ReportManifest", "render_run")


def __getattr__(name: str) -> object:
    if name == "render_run":
        from herness.reports.render import render_run  # noqa: PLC0415 - lazy, see module doc

        return render_run
    raise AttributeError(name)
