"""Stage ``promote`` of ``build_pipeline`` (impl 02 U02-103, T02-21; TH02-17).

Private sibling of ``herness.model.build`` (impl 02 §2 module-map note), split off for the
400-line budget of ``build.py``. ``build`` imports this module; this module reaches
``build`` only inside functions, so there is no import cycle at module level. It also
holds the pre-build ``cleanup_builds`` call of U02-98 step 4.
"""

from __future__ import annotations

import datetime
from typing import TYPE_CHECKING

from herness.core import time as clock
from herness.model import _build_dq, promote

if TYPE_CHECKING:
    from herness.model.build import StageStatus, _BuildRun
    from herness.model.settings import BuildSettings
    from herness.store.layout import DataLayout


def cleanup_before_build(
    build_id: str, build_cfg: BuildSettings, layout: DataLayout, now: datetime.datetime
) -> promote.CleanupReport:
    """U02-98 step 4: ``cleanup_builds(mode="pre")`` protecting this build (and ``CURRENT``,
    which ``cleanup_builds`` always keeps)."""
    return promote.cleanup_builds(
        mode="pre",
        keep_last=build_cfg.keep_last,
        protect=frozenset({build_id}),
        layout=layout,
        now=now,
    )


def stage_promote(run: _BuildRun) -> StageStatus:
    """Stage ``promote``: the DQ gate of this job, then ``promote_build`` (U02-103).

    When ``dq`` did not run in this job (``--from-stage promote``, a retried or resumed job)
    stage ``dq`` runs first, so a promotion never rests on stale DQ results; its gate
    failure raises ``DqGateFailed`` and a yield inside it returns ``yield`` (no promotion).
    ``promote_build`` closes the build connection, which the run then drops.
    """
    from herness.model import build  # noqa: PLC0415 - build imports this module

    if "dq" not in run.result and _build_dq.stage_dq(run) == "yield":
        return "yield"
    con = build._connection(run)
    promote.promote_build(
        con,
        run.build_id,
        build_cfg=run.cfg.sources.build,
        layout=run.layout,
        now=clock.now(),
    )
    run.con = None
    return "done"
