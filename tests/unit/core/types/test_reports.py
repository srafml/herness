"""Tests for herness.core.types.reports (T09-01)."""

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from herness.core.types import ReportManifest

pytestmark = pytest.mark.unit

_RUN_ID = "run_01ARZ3NDEKTSV4RRFFQ69G5FAV"
_HASH = "a" * 64


def _base(**overrides: object) -> dict[str, object]:
    fields: dict[str, object] = {
        "run_id": _RUN_ID,
        "kind": "funding_review",
        "build_id": "20260101-120000-AB1CDE",
        "template_version": "1",
        "rendered_at": datetime(2026, 1, 1, tzinfo=UTC),
        "publishable": True,
        "files": {"report.html": _HASH, "report.md": _HASH},
        "numbers_total": 10,
        "numbers_linked": 5,
        "evidence_entries": 3,
    }
    fields.update(overrides)
    return fields


def test_ut09_01_valid_manifest_round_trips() -> None:
    """UT09-01 a valid dict constructs; defaults for uncited and warnings are empty."""
    manifest = ReportManifest(**_base())
    assert manifest.uncited == []
    assert manifest.warnings == []
    assert manifest.numbers_linked == 5
    with pytest.raises(ValidationError):
        manifest.numbers_total = 1  # type: ignore[misc]


def test_ut09_01_extra_key_rejected() -> None:
    """UT09-01 an unknown field raises ValidationError (extra='forbid')."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(nope="x"))


def test_ut09_01_naive_datetime_rejected() -> None:
    """UT09-01 a naive rendered_at raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(rendered_at=datetime(2026, 1, 1)))  # noqa: DTZ001 - the point


def test_ut09_01_bad_hash_rejected() -> None:
    """UT09-01 a non-hex or wrong-length files value raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(files={"report.html": "not-a-hash"}))
    with pytest.raises(ValidationError):
        ReportManifest(**_base(files={"report.html": "a" * 63}))


def test_ut09_01_unknown_file_key_rejected() -> None:
    """UT09-01 a files key outside the allowed set raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(files={"report.txt": _HASH}))


def test_ut09_01_linked_over_total_rejected() -> None:
    """UT09-01 numbers_linked greater than numbers_total raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(numbers_total=1, numbers_linked=2))


def test_ut09_01_bad_run_id_rejected() -> None:
    """UT09-01 a run_id not matching RUN_ID_RE raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(run_id="not-a-run-id"))


def test_ut09_01_uncited_entries_validated() -> None:
    """UT09-01 each uncited entry must have exactly the keys where and text."""
    manifest = ReportManifest(**_base(uncited=[{"where": "sections[0]", "text": "42%"}]))
    assert manifest.uncited == [{"where": "sections[0]", "text": "42%"}]
    with pytest.raises(ValidationError):
        ReportManifest(**_base(uncited=[{"where": "x"}]))
    with pytest.raises(ValidationError):
        ReportManifest(**_base(uncited=[{"where": "x", "text": "y", "extra": "z"}]))


def test_ut09_01_warning_length_enforced() -> None:
    """UT09-01 a warning over 500 chars raises ValidationError."""
    with pytest.raises(ValidationError):
        ReportManifest(**_base(warnings=["x" * 501]))
    manifest = ReportManifest(**_base(warnings=["x" * 500]))
    assert manifest.warnings == ["x" * 500]
