"""Drift guard for the `metrics_tiny` core DDL (impl 04 T04-06, fixture of UT04-30 ... UT04-35).

`tests/support/metrics_tiny.py: CORE_DDL` repeats the `core.*` column shapes that files
200-280 build; this test builds stages 000-299 on an empty lake and compares them.
"""

import pytest
from tests.integration.model._core_build import build_core
from tests.support.build_harness import BuildHarness
from tests.support.metrics_tiny import CORE_DDL, build_metrics_tiny

pytestmark = pytest.mark.integration

_SHAPES = (
    "SELECT table_schema || '.' || table_name, column_name, data_type"
    " FROM information_schema.columns WHERE table_schema = 'core'"
    " ORDER BY table_name, ordinal_position"
)


def _shapes(rows: list[tuple[object, ...]]) -> dict[str, list[tuple[str, str]]]:
    out: dict[str, list[tuple[str, str]]] = {}
    for table, column, data_type in rows:
        out.setdefault(str(table), []).append((str(column), str(data_type)))
    return out


def test_ut04_35_core_ddl_matches_model_build(build_harness: BuildHarness) -> None:
    """UT04-35 (fixture guard) every core.* table of an empty-lake 000-299 build has exactly the
    columns and types of metrics_tiny's CORE_DDL, and CORE_DDL names no other table."""
    build_core(build_harness, hi=299)
    built = _shapes(build_harness.query(_SHAPES))
    tiny = build_metrics_tiny(rows=False)
    try:
        fixture = _shapes(tiny.execute(_SHAPES).fetchall())
    finally:
        tiny.close()
    assert sorted(fixture) == sorted(CORE_DDL)
    assert fixture == built
