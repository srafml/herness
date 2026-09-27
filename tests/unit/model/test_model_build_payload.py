"""Unit tests for the `build_pipeline` payload and build settings view (impl 02 UT02-65).

UT02-65 covers `STAGE_ORDER` and `BuildPipelinePayload` (U02-95, U02-96). The build memory
limit resolution (T02-18 spec note: DuckDB rejects the `75%` default of `BuildSettings`) is
tested under the same card ID.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from herness.core.errors import ConfigError
from herness.model import _build_support as support
from herness.model.build import STAGE_ORDER, BuildPipelinePayload

pytestmark = pytest.mark.unit

BUILD_ID = "20260901-060000-01ABCD"
GIB = 1024**3


def test_ut02_65_stage_order() -> None:
    """UT02-65 the five stages of spec 08 §7 in order."""
    assert STAGE_ORDER == ("build", "enrich", "score", "dq", "promote")


@pytest.mark.parametrize(
    "payload",
    [
        {"stages": ["build", "score"]},  # not consecutive
        {"stages": ["build"], "build_id": BUILD_ID},  # build with an ID
        {"stages": ["enrich"]},  # no build and no ID
        {"stages": ["enrich"], "build_id": BUILD_ID, "enrich_stage": "Bad Stage"},
        {"stages": ["build"], "enrich_stage": "link"},  # enrich_stage without enrich
        {"stages": ["build"], "score_steps": ["facts"]},  # score_steps without score
        {"stages": ["enrich", "score"], "build_id": "not-an-id"},
        {"stages": []},
        {"stages": ["build", "build"]},
        {"stages": ["score", "enrich"], "build_id": BUILD_ID},
        {"stages": ["build"], "extra": 1},
        {"stages": ["build"], "depth": "huge"},
        {"stages": ["score"], "build_id": BUILD_ID, "score_steps": ["Bad-Step"]},
        {"stages": ["score"], "build_id": BUILD_ID, "score_steps": []},
    ],
)
def test_ut02_65_invalid_payloads(payload: dict[str, Any]) -> None:
    """UT02-65 non-consecutive stages, build with an ID, enrich_stage without enrich, a bad
    enrich_stage and the other invariants are rejected."""
    with pytest.raises(ValidationError):
        BuildPipelinePayload.model_validate(payload)


def test_ut02_65_valid_payloads() -> None:
    """UT02-65 `[enrich, score, dq, promote]` with an ID and `[enrich]` with
    `enrich_stage="link"` validate; defaults apply; the model is frozen."""
    full = BuildPipelinePayload.model_validate(
        {"stages": ["enrich", "score", "dq", "promote"], "build_id": BUILD_ID}
    )
    assert full.stages == ["enrich", "score", "dq", "promote"]
    assert (full.depth, full.rekey_night, full.score_steps) == ("standard", False, None)
    link = BuildPipelinePayload.model_validate(
        {"stages": ["enrich"], "build_id": BUILD_ID, "enrich_stage": "link"}
    )
    assert link.enrich_stage == "link"
    scored = BuildPipelinePayload.model_validate(
        {"stages": ["score"], "build_id": BUILD_ID, "score_steps": ["facts", "anomaly"]}
    )
    assert scored.score_steps == ["facts", "anomaly"]
    new = BuildPipelinePayload.model_validate(
        {"stages": ["build"], "schedule": "nightly", "fire_at": "2026-09-01T02:00:00Z"}
    )
    assert new.build_id is None
    with pytest.raises(ValidationError):
        new.depth = "deep"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("1GB", "1GB"),
        ("2.5 GiB", "2.5 GiB"),
        ("512MiB", "512MiB"),
        ("50%", f"{(64 * GIB // 2) // (1024 * 1024)}MiB"),
        ("100%", f"{(64 * GIB) // (1024 * 1024)}MiB"),
        ("1%", "256MiB"),  # 1 % of 1 GiB is below the minimum: raised to it
    ],
)
def test_ut02_65_memory_limit_resolution(
    monkeypatch: pytest.MonkeyPatch, value: str, expected: str
) -> None:
    """UT02-65 (T02-18 spec note) byte sizes pass through; a percentage becomes MiB of total
    physical RAM, at least 256 MiB."""
    total = GIB if value == "1%" else 64 * GIB
    monkeypatch.setattr(support, "_total_memory", lambda: total)
    assert support.resolve_memory_limit(value) == expected


@pytest.mark.parametrize("value", ["0%", "101%", "abc", "10MB", "0.1GB", "", "75 %", "-5%"])
def test_ut02_65_memory_limit_rejects(value: str) -> None:
    """UT02-65 (T02-18 spec note) percentages outside 1..100, unknown formats and sizes below
    256 MiB are ConfigError."""
    with pytest.raises(ConfigError, match="memory_limit"):
        support.resolve_memory_limit(value)
