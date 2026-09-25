"""Tests for herness.store.errors (U02-01 … U02-05), error-class part."""

import pickle  # noqa: TID251 - proves multiprocessing transport of the extra attributes

import pytest

from herness.core import errors as e
from herness.store import errors as se

pytestmark = pytest.mark.unit


def test_ut02_45_not_found_error_fields() -> None:
    """UT02-45 (error-class part) NotFoundError is a NotFound with kind/key details."""
    err = se.NotFoundError("review_item ri_1 not found", kind="review_item", key="ri_1")
    assert isinstance(err, e.NotFound)
    assert isinstance(err, e.RecoverableError)
    assert str(err) == "review_item ri_1 not found"
    assert (err.kind, err.key) == ("review_item", "ri_1")
    assert dict(err.details) == {"kind": "review_item", "key": "ri_1"}
    assert err.hint is None
    copy = pickle.loads(pickle.dumps(err))  # noqa: S301 - own object round trip
    assert (type(copy), copy.kind, copy.key, dict(copy.details)) == (
        se.NotFoundError,
        "review_item",
        "ri_1",
        {"kind": "review_item", "key": "ri_1"},
    )


def test_ut02_45_review_item_conflict_message() -> None:
    """UT02-45 (error-class part) ReviewItemConflict names the item and its status."""
    err = se.ReviewItemConflict("ri_1", "approved")
    assert isinstance(err, e.RecoverableError)
    assert str(err) == "review item ri_1 is already approved"
    assert (err.item_id, err.current_status) == ("ri_1", "approved")


def test_ut02_02_lake_contract_error_message() -> None:
    """UT02-02 (error-class part) LakeContractError is a SchemaViolation with rule fields."""
    err = se.LakeContractError("jira", "issue", "missing_column", "_deleted", 0)
    assert isinstance(err, e.SchemaViolation)
    assert str(err) == "lake contract violated for jira/issue: missing_column on _deleted (0 rows)"
    assert (err.source, err.entity, err.rule, err.column, err.bad_rows) == (
        "jira",
        "issue",
        "missing_column",
        "_deleted",
        0,
    )


def test_ut02_10_lake_state_error_message() -> None:
    """UT02-10 (error-class part) LakeStateError is fatal and names state and method."""
    err = se.LakeStateError("jira", "issue", "aborted", "write")
    assert isinstance(err, e.FatalError)
    assert str(err) == "LakeWriter for jira/issue is aborted; write() not allowed"
    assert (err.source, err.entity, err.state, err.method) == ("jira", "issue", "aborted", "write")


def test_ut02_34_migration_error_message() -> None:
    """UT02-34 (error-class part) MigrationError message pads the version to three digits."""
    err = se.MigrationError(3, "runs_evidence", "checksum_mismatch", "stored != file")
    assert isinstance(err, e.SchemaViolation)
    assert str(err) == "ops migration 003_runs_evidence: checksum_mismatch (stored != file)"
    assert (err.version, err.name, err.reason, err.detail) == (
        3,
        "runs_evidence",
        "checksum_mismatch",
        "stored != file",
    )
