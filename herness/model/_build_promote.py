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
from herness.store import warehouse

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


def _promoted_current(run: _BuildRun) -> bool:
    """``CURRENT`` names this build and its status is ``promoted``: the switch is done."""
    builds = warehouse.list_builds(layout=run.layout)
    return any(
        b.build_id == run.build_id and b.is_current for b in builds if b.status == "promoted"
    )


def keeps_status(run: _BuildRun, stage: str) -> bool:
    """U02-98 error path (T02-21): an error in stage ``promote`` never marks the build
    ``failed`` once this job's gate passed or the build is the promoted ``CURRENT``, so the
    retry resumes at ``promote`` (``resolve_build`` resumes a ``promoted`` build)."""
    return stage == "promote" and ("dq" in run.result or _promoted_current(run))


def stage_promote(run: _BuildRun) -> StageStatus:
    """Stage ``promote``: the DQ gate of this job, then ``promote_build`` (U02-103).

    When ``dq`` did not run in this job (``--from-stage promote``, a retried or resumed job)
    stage ``dq`` runs first, so a promotion never rests on stale DQ results; its gate
    failure raises ``DqGateFailed`` and a yield inside it returns ``yield`` (no promotion).
    ``promote_build`` closes the build connection, which the run then drops. A retry whose
    earlier attempt already switched ``CURRENT`` to this ``promoted`` build only finishes
    the promotion (retention and the log): no gate, no second switch (T02-21 spec note).
    """
    from herness.model import build  # noqa: PLC0415 - build imports this module

    cfg, now = run.cfg.sources.build, clock.now()
    if _promoted_current(run):
        promote.finish_promotion(
            run.build_id, build_cfg=cfg, layout=run.layout, now=now, previous=None
        )
        return "done"
    if "dq" not in run.result and _build_dq.stage_dq(run) == "yield":
        return "yield"
    promote.promote_build(
        build._connection(run), run.build_id, build_cfg=cfg, layout=run.layout, now=now
    )
    run.con = None
    return "done"
