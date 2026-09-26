"""Build helper for the later core files 260-280 (impl 02 T02-17).

`build_late` runs files 000..220 (staging, org/team/service, service map) with reference
data from a `mappings.yaml`-shaped dict and the given custom fields, calls `setup` on the
build connection, then runs each requested later file on its own. Files 230-250 (T02-16)
are never run: a test that needs `core.incident` creates a minimal one in `setup`.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

import duckdb
from tests.support.build_harness import BuildHarness, RefData

from herness.model.lakeinfo import scan_lake
from herness.model.settings import CustomFieldsConfig, MappingsConfig

_CORE_BASE = 220


def build_late(
    harness: BuildHarness,
    files: Sequence[int],
    *,
    mappings: Mapping[str, object] | None = None,
    custom_fields: Mapping[str, Mapping[str, str]] | None = None,
    setup: Callable[[duckdb.DuckDBPyConnection], None] | None = None,
) -> list[str]:
    """Run files 000..220, then `setup(con)`, then each file of `files`; return names run."""
    refdata = RefData(mappings=MappingsConfig.model_validate(dict(mappings or {})))
    fields = CustomFieldsConfig.model_validate(dict(custom_fields or {}))
    context = harness.context(scan_lake(harness.layout), custom_fields=fields)
    ran = harness.run(0, _CORE_BASE, refdata=refdata, context=context)
    if setup is not None:
        setup(harness.con)
    for number in files:
        ran += harness.run(number, number, context=context)
    return ran


def rerun(harness: BuildHarness, number: int) -> list[str]:
    """Run one later file again on the same build (idempotence checks)."""
    return harness.run(number, number)
