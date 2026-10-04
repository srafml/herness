"""Security test ST06-16 (TH06-16): many near-identical findings must not make dedup quadratic.

1,000 near-identical findings on one entity: `dedup_components` compares at most the first
`max_group` = 200 of the group (asserted structurally by counting pair comparisons and the
findings they touch) and finishes in under 5 s.
"""

from __future__ import annotations

import time

import numpy as np
import pytest
from tests.unit.harness.swarm._swarm_findings import finding

from herness.core.types import Finding
from herness.harness.swarm import dedup

pytestmark = pytest.mark.unit

N_FINDINGS = 1_000
MAX_GROUP = 200


def _flood() -> list[Finding]:
    return [
        finding(i, confidence=(i % 97) / 97, claim=f"Team t1 had [[n1]] incidents ({i})")
        for i in range(1, N_FINDINGS + 1)
    ]


def _vectors(fs: list[Finding]) -> dict[str, np.ndarray]:
    rng = np.random.default_rng(16)
    base = np.ones(1024, dtype=np.float32)
    out: dict[str, np.ndarray] = {}
    for f in fs:
        v = base + rng.normal(0, 0.01, 1024).astype(np.float32)
        out[f.finding_id] = v / np.linalg.norm(v)
    return out


@pytest.mark.parametrize("mode", ["query_set", "embedding"])
def test_st06_16_dedup_compares_at_most_200(mode: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """ST06-16 1,000 near-identical findings: ≤ 200 findings compared, ≤ 200·199/2 pairs."""
    fs = _flood()
    vectors = _vectors(fs) if mode == "embedding" else None
    touched: set[str] = set()
    pairs = 0
    real = dedup._linked

    def counting(a: dedup._Item, b: dedup._Item, link: dedup._Link) -> bool:
        nonlocal pairs
        pairs += 1
        touched.update({a.finding.finding_id, b.finding.finding_id})
        return real(a, b, link)

    monkeypatch.setattr(dedup, "_linked", counting)
    out = dedup.dedup_components(fs, mode=mode, vectors=vectors, cosine=0.85)  # type: ignore[arg-type]
    assert len(touched) <= MAX_GROUP
    assert pairs <= MAX_GROUP * (MAX_GROUP - 1) // 2
    assert len(out) == 1
    keep, dups = out[0]
    assert len(dups) == MAX_GROUP - 1
    assert {keep, *dups} == touched


@pytest.mark.parametrize("mode", ["query_set", "embedding"])
def test_st06_16_dedup_finishes_under_5_s(mode: str) -> None:
    """ST06-16 the 1,000-finding flood is deduplicated in under 5 s."""
    fs = _flood()
    vectors = _vectors(fs) if mode == "embedding" else None
    started = time.perf_counter()
    out = dedup.dedup_components(fs, mode=mode, vectors=vectors, cosine=0.85)  # type: ignore[arg-type]
    elapsed = time.perf_counter() - started
    assert elapsed < 5.0
    assert len(out[0][1]) == MAX_GROUP - 1
