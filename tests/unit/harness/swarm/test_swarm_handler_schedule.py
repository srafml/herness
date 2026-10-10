"""UT06-96: the scheduled `review` payload shape of `config/resilience.yaml` (impl 06 U06-85)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml
from tests.unit.harness.swarm.test_swarm_handler import FakeSwarm, _run, fake

from herness.core.errors import ConfigError

pytestmark = pytest.mark.unit

__all__ = ["fake"]  # the fixture of the UT06-56 module
SHIPPED = Path(__file__).resolve().parents[4] / "config" / "resilience.yaml"
_FIRE = {"schedule": "nightly", "fire_at": "2026-09-20T01:00:00.000000Z", "step": 1}


def _shipped_review_payloads() -> list[dict[str, Any]]:
    """Every `review` payload of the shipped schedule, as the scheduler submits it (U08-73)."""
    jobs = yaml.safe_load(SHIPPED.read_text(encoding="utf-8"))["schedule"]["jobs"]
    steps = [s for j in jobs for s in [j["job"], *j.get("then", [])]]
    return [s["payload"] | _FIRE for s in steps if s["kind"] == "review"]


def test_ut06_96_shipped_review_payloads_start_a_run(fake: FakeSwarm) -> None:
    """UT06-96 every review payload of `config/resilience.yaml` is accepted and starts one run
    of its pipeline at its depth."""
    payloads = _shipped_review_payloads()
    assert len(payloads) >= 3
    for payload in payloads:
        fake.calls.clear()
        out = _run(payload)
        ((kind, req, job),) = fake.calls
        assert (kind, req.kind, req.depth) == ("start", payload["pipeline"], payload["depth"])
        assert job is not None
        assert out.status == "done"


@pytest.mark.parametrize(
    "payload",
    [
        {"depth": "standard"},
        {"pipeline": "nope", "depth": "standard"},
        {"pipeline": "chat", "depth": "standard"},
        {"pipeline": "funding_review", "depth": "huge"},
        {"pipeline": "funding_review", "depth": 2},
        {"pipeline": ["funding_review"], "depth": "deep"},
        {"pipeline": 7},
        {"pipelines": ["funding_review", "org_review"], "depth": "deep"},
        {"pipeline": "funding_review", "depth": "deep", "question": "why"},
        {"pipeline": "funding_review", "depth": "deep", "step": "one"},
        {"pipeline": "funding_review", "depth": "deep", "fire_at": 5},
        {"pipeline": "funding_review", "request": {"kind": "funding_review"}},
        {"pipeline": "funding_review", "run_id": "run_" + "0" * 26, "resume": True},
    ],
)
def test_ut06_96_malformed_scheduled_payload_config_error(
    fake: FakeSwarm, payload: dict[str, Any]
) -> None:
    """UT06-96 a malformed scheduled payload fails closed with `ConfigError`, no run starts."""
    with pytest.raises(ConfigError, match=r"^review payload invalid: job_id=job_1{26}$"):
        _run(payload)
    assert fake.calls == []
    assert fake.closed == 1


def test_ut06_96_depth_defaults_to_standard(fake: FakeSwarm) -> None:
    """UT06-96 a scheduled payload without `depth` runs at standard depth."""
    _run({"pipeline": "org_review"})
    assert fake.calls[0][1].depth == "standard"
