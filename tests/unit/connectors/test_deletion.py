"""Tests for herness.connectors.deletion.DeletionFilter (U01-33)."""

from __future__ import annotations

import datetime

import pyarrow as pa
import pytest
from hypothesis import given
from hypothesis import strategies as st
from tests.support.ops_store import OpsStoreHandle

import herness.connectors.deletion as deletion_module
from herness.connectors.deletion import DeletionFilter
from herness.core.errors import SchemaViolation
from herness.store.ops.privacy import create_deletion_request, set_deletion_status

pytestmark = pytest.mark.unit

_NOW = datetime.datetime(2026, 1, 1, tzinfo=datetime.UTC)
_RECORD_ID_SCHEMA: pa.Schema = pa.schema([pa.field("_record_id", pa.string(), nullable=False)])


def _seed_running_deletion(record_id: str) -> None:
    request = create_deletion_request(
        record_id=record_id, requested_by="a" * 32, reason_ref="ticket-1", now=_NOW
    )
    set_deletion_status(request.request_id, "running")


def _batch(record_ids: list[str]) -> pa.RecordBatch:
    return pa.RecordBatch.from_pylist(
        [{"_record_id": rid} for rid in record_ids], schema=_RECORD_ID_SCHEMA
    )


def test_ut01_25_apply_before_reload_raises(ops_store: OpsStoreHandle) -> None:
    """UT01-25 apply before any reload raises SchemaViolation naming the unloaded set."""
    deletion = DeletionFilter("src", "ent")
    with pytest.raises(SchemaViolation) as info:
        deletion.apply(_batch(["src:ent:a"]))
    assert info.value.message == "deletion set not loaded"


def test_ut01_25_apply_drops_deleted_ids(ops_store: OpsStoreHandle) -> None:
    """UT01-25 two deleted ids in a batch of five drop exactly those two rows after reload."""
    _seed_running_deletion("src:ent:a")
    _seed_running_deletion("src:ent:b")
    deletion = DeletionFilter("src", "ent")
    assert deletion.reload() == 2
    assert deletion.size == 2

    batch = _batch(["src:ent:a", "src:ent:b", "src:ent:c", "src:ent:d", "src:ent:e"])
    filtered, dropped = deletion.apply(batch)

    assert dropped == 2
    assert filtered.num_rows == 3
    assert filtered.column("_record_id").to_pylist() == ["src:ent:c", "src:ent:d", "src:ent:e"]


def test_ut01_25_apply_empty_set_passthrough(ops_store: OpsStoreHandle) -> None:
    """UT01-25 an empty deletion set passes the batch through unchanged, dropping nothing."""
    deletion = DeletionFilter("src", "otherent")
    assert deletion.reload() == 0
    assert deletion.size == 0

    batch = _batch(["src:otherent:a", "src:otherent:b"])
    filtered, dropped = deletion.apply(batch)

    assert dropped == 0
    assert filtered is batch


def test_ut01_25_reload_only_matches_source_and_entity(ops_store: OpsStoreHandle) -> None:
    """UT01-25 deletion requests for another source/entity never count toward this filter."""
    _seed_running_deletion("other:ent:a")
    deletion = DeletionFilter("src", "ent")
    assert deletion.reload() == 0


_RECORD_ID = st.from_regex(
    r"[a-z][a-z0-9_]{0,10}:[a-z][a-z0-9_]{0,10}:[A-Za-z0-9._:@/+-]{1,20}", fullmatch=True
)


@given(st.sets(_RECORD_ID, max_size=15), st.lists(_RECORD_ID, max_size=25))
def test_pt01_06_apply_never_lets_a_deleted_id_through(
    deleted_ids: set[str], batch_ids: list[str]
) -> None:
    """PT01-06 output never contains a deleted id, and rows_out + dropped == rows_in."""
    original = deletion_module.deleted_record_ids  # type: ignore[attr-defined]
    deletion_module.deleted_record_ids = (  # type: ignore[attr-defined]
        lambda _source, _entity: sorted(deleted_ids)  # type: ignore[assignment]
    )
    try:
        deletion = DeletionFilter("src", "ent")
        deletion.reload()
        batch = _batch(batch_ids)

        filtered, dropped = deletion.apply(batch)

        kept = set(filtered.column("_record_id").to_pylist())
        assert kept.isdisjoint(deleted_ids)
        assert filtered.num_rows + dropped == len(batch_ids)
    finally:
        deletion_module.deleted_record_ids = original  # type: ignore[attr-defined]
