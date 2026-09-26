"""Generic configured-entity staging `140_stg_files.sql`, `150_stg_mongodb.sql`,
`160_stg_snowflake.sql`, `170_stg_dataverse.sql` (impl 02 U02-112…U02-115, T02-14, T02-15).

IT02-33 is this card's only Tests-row ID (`docs/impl/02-data-model.impl.md` line ~3903,
scenario at line ~3655: "configured `files/service_costs` entity -> build -> stg.
files_service_costs with latest rows and no `_payload`"); every function below is filed
under it, including the acceptance check "a build with `extra_entities` for each source
renders" (line ~3905).

U02-115 (`170_stg_dataverse.sql`, T02-15) follows the ID the base uses for 150/160.

Lake files are written with the real `LakeWriter`; each test builds files 000-170 on a
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

    build(build_harness, extra_entities={"files": ["service_costs"]}, lo=0, hi=170)

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
    build(build_harness, extra_entities={"files": ["service_costs"]}, lo=0, hi=170)

    cols = columns(build_harness, "files_service_costs")
    assert [name for name, _ in cols] == ["record_id", "source_key", "source_updated_at"]
    assert build_harness.query("SELECT count(*) FROM stg.files_service_costs") == [(0,)]


def test_it02_33_mongodb_snowflake_dataverse_entities_stage(
    build_harness: BuildHarness,
) -> None:
    """IT02-33 (U02-113…U02-115): configured `mongodb`/`snowflake`/`dataverse` entities
    stage the same way as `files` (U02-112): latest rows, lake columns, no `_payload`, no
    casting."""
    raw = build_harness.layout.raw
    commit(raw, "mongodb", "orders", [Row("o1", at(0), {"qty": "3", "sku": "SKU1"})])
    commit(raw, "snowflake", "usage", [Row("u1", at(0), {"credits": "12.5"})])
    commit(raw, "dataverse", "accounts", [Row("a1", at(0), {"name": "Acme", "tier": "1"})])
    commit(raw, "dataverse", "accounts", [Row("a1", at(2), {"name": "Acme Ltd", "tier": "2"})])
    commit(raw, "dataverse", "accounts", [Row("a2", at(0), {"name": "Gone"})])
    commit(raw, "dataverse", "accounts", [Row("a2", at(1), deleted=True)])

    build(
        build_harness,
        extra_entities={"mongodb": ["orders"], "snowflake": ["usage"], "dataverse": ["accounts"]},
        lo=0,
        hi=170,
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

    dv_cols = [name for name, _ in columns(build_harness, "dataverse_accounts")]
    assert dv_cols == ["record_id", "source_key", "source_updated_at", "name", "tier"]
    assert build_harness.query(
        "SELECT record_id, source_key, source_updated_at, name, tier FROM stg.dataverse_accounts"
    ) == [("dataverse:accounts:a1", "a1", at(2), "Acme Ltd", "2")]


def test_it02_33_dataverse_absent_entity_and_rerun(build_harness: BuildHarness) -> None:
    """IT02-33 (U02-115): a configured `dataverse` entity with no lake files gives a table
    with only the metadata aliases and no rows; re-running 170 keeps it that way."""
    build(build_harness, extra_entities={"dataverse": ["contacts"]}, lo=0, hi=170)
    build(build_harness, extra_entities={"dataverse": ["contacts"]}, lo=170, hi=170)

    cols = columns(build_harness, "dataverse_contacts")
    assert [name for name, _ in cols] == ["record_id", "source_key", "source_updated_at"]
    assert build_harness.query("SELECT count(*) FROM stg.dataverse_contacts") == [(0,)]


def test_it02_33_extra_entities_for_each_source_renders(build_harness: BuildHarness) -> None:
    """IT02-33 acceptance check: "a build with `extra_entities` for each source renders" -
    140-170 render without error when `extra_entities` lists an entity for every
    config-defined source (files, mongodb, snowflake, dataverse), even with no lake data
    behind any of them."""
    inventory: LakeInventory = scan_lake(
        build_harness.layout,
        extra_entities=[
            ("files", "service_costs"),
            ("mongodb", "orders"),
            ("snowflake", "usage"),
            ("dataverse", "accounts"),
        ],
    )
    context = build_harness.context(
        inventory,
        extra_entities={
            "files": ("service_costs",),
            "mongodb": ("orders",),
            "snowflake": ("usage",),
            "dataverse": ("accounts",),
        },
    )
    rendered = build_harness.render(130, 170, context=context)
    assert set(rendered) == {
        "130_stg_monitoring.sql",
        "140_stg_files.sql",
        "150_stg_mongodb.sql",
        "160_stg_snowflake.sql",
        "170_stg_dataverse.sql",
    }
    assert 'stg."files_service_costs"' in rendered["140_stg_files.sql"]
    assert 'stg."mongodb_orders"' in rendered["150_stg_mongodb.sql"]
    assert 'stg."snowflake_usage"' in rendered["160_stg_snowflake.sql"]
    assert 'stg."dataverse_accounts"' in rendered["170_stg_dataverse.sql"]
