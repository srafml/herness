"""Generic configured-entity staging `140_stg_files.sql`, `150_stg_mongodb.sql`,
`160_stg_snowflake.sql` (impl 02 U02-112…U02-114, T02-14).

IT02-33 is this card's only Tests-row ID (`docs/impl/02-data-model.impl.md` line ~3903,
scenario at line ~3655: "configured `files/service_costs` entity -> build -> stg.
files_service_costs with latest rows and no `_payload`"); every function below is filed
under it, including the acceptance check "a build with `extra_entities` for each source
renders" (line ~3905).

Lake files are written with the real `LakeWriter`; each test builds files 000-160 on a
temp DuckDB build through `build_harness` with real reference tables (U02-87).
"""

from __future__ import annotations

import pytest
from tests.integration.model._stg_lake import Row, at, build, columns, commit
from tests.support.build_harness import BuildHarness

from herness.model.lakeinfo import LakeInventory, scan_lake

pytestmark = pytest.mark.integration


def test_it02_33_files_service_costs_latest_rows_no_payload(build_harness: BuildHarness) -> None:
    """IT02-33: configured `files/service_costs` entity -> build -> `stg.files_service_costs`
    with latest rows and no `_payload` (nor any other metadata column)."""
    raw = build_harness.layout.raw
    v1 = {"cost_center": "CC1", "amount": "100.00", "currency": "USD"}
    v2 = {"cost_center": "CC1", "amount": "150.00", "currency": "USD"}
    commit(raw, "files", "service_costs", [Row("row1", at(0), v1)])
    commit(raw, "files", "service_costs", [Row("row1", at(1), v2)])
    commit(raw, "files", "service_costs", [Row("row2", at(0), {"cost_center": "CC2"})])

    build(build_harness, extra_entities={"files": ["service_costs"]}, lo=0, hi=160)

    cols = columns(build_harness, "files_service_costs")
    names = [name for name, _ in cols]
    assert names == [
        "record_id",
        "source_key",
        "source_updated_at",
        "amount",
        "cost_center",
        "currency",
    ]
    assert "_payload" not in names
    rows = build_harness.query(
        "SELECT record_id, source_key, source_updated_at, amount, cost_center, currency"
        " FROM stg.files_service_costs ORDER BY source_key"
    )
    assert rows == [
        (
            "files:service_costs:row1",
            "row1",
            at(1),
            "150.00",
            "CC1",
            "USD",
        ),
        (
            "files:service_costs:row2",
            "row2",
            at(0),
            None,
            "CC2",
            None,
        ),
    ]


def test_it02_33_files_absent_entity_metadata_columns_only(build_harness: BuildHarness) -> None:
    """IT02-33: a configured `files` entity with zero lake files renders a table with only
    the metadata aliases, no data columns, no rows (schema drift, design 02 §11)."""
    build(build_harness, extra_entities={"files": ["service_costs"]}, lo=0, hi=160)

    cols = columns(build_harness, "files_service_costs")
    assert [name for name, _ in cols] == ["record_id", "source_key", "source_updated_at"]
    assert build_harness.query("SELECT count(*) FROM stg.files_service_costs") == [(0,)]


def test_it02_33_mongodb_and_snowflake_entities_stage(build_harness: BuildHarness) -> None:
    """IT02-33 (U02-113, U02-114): configured `mongodb`/`snowflake` entities stage the same
    way as `files` (U02-112): latest rows, lake columns, no `_payload`, no casting."""
    raw = build_harness.layout.raw
    commit(raw, "mongodb", "orders", [Row("o1", at(0), {"qty": "3", "sku": "SKU1"})])
    commit(raw, "snowflake", "usage", [Row("u1", at(0), {"credits": "12.5"})])

    build(
        build_harness,
        extra_entities={"mongodb": ["orders"], "snowflake": ["usage"]},
        lo=0,
        hi=160,
    )

    mongo_cols = [name for name, _ in columns(build_harness, "mongodb_orders")]
    assert mongo_cols == ["record_id", "source_key", "source_updated_at", "qty", "sku"]
    assert build_harness.query(
        "SELECT record_id, source_key, qty, sku FROM stg.mongodb_orders"
    ) == [("mongodb:orders:o1", "o1", "3", "SKU1")]

    snow_cols = [name for name, _ in columns(build_harness, "snowflake_usage")]
    assert snow_cols == ["record_id", "source_key", "source_updated_at", "credits"]
    assert build_harness.query(
        "SELECT record_id, source_key, credits FROM stg.snowflake_usage"
    ) == [("snowflake:usage:u1", "u1", "12.5")]


def test_it02_33_extra_entities_for_each_source_renders(build_harness: BuildHarness) -> None:
    """IT02-33 acceptance check: "a build with `extra_entities` for each source renders" -
    140-160 render without error when `extra_entities` lists an entity for every
    config-defined source this card covers (files, mongodb, snowflake), even with no lake
    data behind any of them."""
    inventory: LakeInventory = scan_lake(
        build_harness.layout,
        extra_entities=[("files", "service_costs"), ("mongodb", "orders"), ("snowflake", "usage")],
    )
    context = build_harness.context(
        inventory,
        extra_entities={
            "files": ("service_costs",),
            "mongodb": ("orders",),
            "snowflake": ("usage",),
        },
    )
    rendered = build_harness.render(130, 160, context=context)
    assert set(rendered) == {
        "130_stg_monitoring.sql",
        "140_stg_files.sql",
        "150_stg_mongodb.sql",
        "160_stg_snowflake.sql",
    }
    assert 'stg."files_service_costs"' in rendered["140_stg_files.sql"]
    assert 'stg."mongodb_orders"' in rendered["150_stg_mongodb.sql"]
    assert 'stg."snowflake_usage"' in rendered["160_stg_snowflake.sql"]
