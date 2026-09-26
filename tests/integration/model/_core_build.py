"""Build helper for the core-stage integration tests (impl 02 T02-15).

`build_core` scans the harness lake and runs files lo..hi with reference data built from a
`mappings.yaml`-shaped dict, the configured service CI classes and approved mapping
suggestions (U02-87), the way the pipeline registers them before file 100.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from tests.support.build_harness import HARNESS_BUILD_SETTINGS, BuildHarness, RefData

from herness.model.lakeinfo import scan_lake
from herness.model.settings import MappingsConfig
from herness.store.ops import ReviewItem

SVC = "servicenow:cmdb_ci:"
TEAM = "servicenow:sys_user_group:"
DEPT = "servicenow:cmn_department:"


def build_core(
    harness: BuildHarness,
    *,
    mappings: Mapping[str, object] | None = None,
    approved: Sequence[ReviewItem] = (),
    ci_classes: Sequence[str] | None = None,
    hi: int = 220,
) -> list[str]:
    """Scan the harness lake and run files 000..hi; return the names of the files run."""
    build_cfg = HARNESS_BUILD_SETTINGS
    if ci_classes is not None:
        build_cfg = build_cfg.model_copy(update={"service_ci_classes": list(ci_classes)})
    refdata = RefData(
        mappings=MappingsConfig.model_validate(dict(mappings or {})),
        build_cfg=build_cfg,
        approved=approved,
    )
    context = harness.context(scan_lake(harness.layout))
    return harness.run(0, hi, refdata=refdata, context=context)
