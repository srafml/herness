"""UT06-69, PT06-03: dedup components (U06-102); `run_dedup` modes and merges (U06-103)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast

import numpy as np
import pytest
import structlog
from hypothesis import given
from hypothesis import strategies as st
from tests.unit.harness.swarm._swarm_findings import finding, number, run_row, tid

from herness.core.errors import ConfigError, ModelUnavailable
from herness.core.types import Finding
from herness.harness.pipelines.settings import PipelinesConfig
from herness.harness.swarm import dedup

if TYPE_CHECKING:
    from herness.harness.blackboard import FindingFilter
    from herness.harness.swarm.routing import RunEnv

pytestmark = pytest.mark.unit


def _unit(*xs: float) -> np.ndarray:
    v = np.array(xs, dtype=np.float32)
    return v / np.linalg.norm(v)


# --- dedup_components (UT06-69) ---------------------------------------------------------------


def test_ut06_69_planted_duplicates_form_one_component() -> None:
    """UT06-69 planted duplicates: one component; keep = highest confidence x numbers."""
    two = [number(1), number(2, column="hours")]
    fs = [
        finding(1, confidence=0.9),  # score 0.9
        finding(2, confidence=0.6, numbers=two),  # score 1.2 -> keep
        finding(3, confidence=0.3),
        finding(4, entity=("team", "t2")),  # other entity: never compared
        finding(5, numbers=[number(1, q=9)]),  # no shared triple
    ]
    out = dedup.dedup_components(fs, mode="query_set", vectors=None, cosine=0.85)
    assert out == [(fs[1].finding_id, [fs[0].finding_id, fs[2].finding_id])]


def test_ut06_69_query_set_mode_needs_equal_query_sets() -> None:
    """UT06-69 query_set mode: a shared triple with a different query set is no edge."""
    a = finding(1, numbers=[number(1), number(2, q=2)])
    b = finding(2, numbers=[number(1)])
    assert dedup.dedup_components([a, b], mode="query_set", vectors=None, cosine=0.85) == []


def test_ut06_69_embedding_mode_uses_cosine_threshold() -> None:
    """UT06-69 embedding mode: edge when the dot product reaches `cosine`; transitive union."""
    fs = [finding(i, confidence=1 - i / 10) for i in range(1, 5)]
    vectors = {
        fs[0].finding_id: _unit(1, 0),
        fs[1].finding_id: _unit(1, 0.1),
        fs[2].finding_id: _unit(0, 1),
        fs[3].finding_id: _unit(0.1, 1),
    }
    out = dedup.dedup_components(fs, mode="embedding", vectors=vectors, cosine=0.95)
    assert out == [
        (fs[0].finding_id, [fs[1].finding_id]),
        (fs[2].finding_id, [fs[3].finding_id]),
    ]
    missing = {k: v for k, v in vectors.items() if k != fs[1].finding_id}
    out = dedup.dedup_components(fs, mode="embedding", vectors=missing, cosine=0.95)
    assert out == [(fs[2].finding_id, [fs[3].finding_id])]


def test_ut06_69_embedding_mode_without_vectors_raises() -> None:
    """UT06-69 embedding mode needs the vectors mapping."""
    with pytest.raises(ConfigError):
        dedup.dedup_components([finding(1)], mode="embedding", vectors=None, cosine=0.85)


def test_ut06_69_ties_break_on_created_at_then_id() -> None:
    """UT06-69 equal scores: the earlier `created_at`, then the lower id, is kept."""
    fs = [finding(3, created=5), finding(2, created=1), finding(1, created=1)]
    out = dedup.dedup_components(fs, mode="query_set", vectors=None, cosine=0.85)
    assert out == [(fs[2].finding_id, [fs[1].finding_id, fs[0].finding_id])]


def test_ut06_69_row_key_is_part_of_the_triple() -> None:
    """UT06-69 the number triple includes the canonical row_key."""
    a = finding(1, numbers=[number(1, row_key={"week": 1})])
    b = finding(2, numbers=[number(1, row_key={"week": 2})])
    c = finding(3, numbers=[number(1, row_key={"week": 1})])
    out = dedup.dedup_components([a, b, c], mode="query_set", vectors=None, cosine=0.85)
    assert out == [(a.finding_id, [c.finding_id])]


def test_ut06_69_max_group_limits_compared_findings() -> None:
    """UT06-69 only the first `max_group` of an entity group are compared."""
    fs = [finding(i, confidence=0.9 - i / 100) for i in range(1, 6)]
    out = dedup.dedup_components(fs, mode="query_set", vectors=None, cosine=0.85, max_group=2)
    assert out == [(fs[0].finding_id, [fs[1].finding_id])]


# --- PT06-03 ----------------------------------------------------------------------------------


@st.composite
def _findings(draw: st.DrawFn) -> list[Finding]:
    count = draw(st.integers(min_value=0, max_value=12))
    out: list[Finding] = []
    for i in range(count):
        n_nums = draw(st.integers(min_value=1, max_value=3))
        nums = [number(k + 1, q=draw(st.integers(1, 2))) for k in range(n_nums)]
        out.append(
            finding(
                i + 1,
                entity=("team", draw(st.sampled_from(["t1", "t2"]))),
                numbers=nums,
                confidence=draw(st.floats(min_value=0, max_value=1)),
                created=draw(st.integers(0, 3)),
            )
        )
    return out


def _score(f: Finding) -> float:
    return f.confidence * len(f.numbers)


@given(fs=_findings(), max_group=st.integers(min_value=1, max_value=12))
def test_pt06_03_output_partitions_compared_findings(fs: list[Finding], max_group: int) -> None:
    """PT06-03 components are disjoint, within the compared set, and keep maximizes the score."""
    out = dedup.dedup_components(fs, mode="query_set", vectors=None, cosine=0.85,
                                 max_group=max_group)  # fmt: skip
    by_id = {f.finding_id: f for f in fs}
    compared: set[str] = set()
    for entity in {(f.entity_type, f.entity_id) for f in fs}:
        group = [f for f in fs if (f.entity_type, f.entity_id) == entity]
        group.sort(key=lambda f: (-_score(f), f.created_at, f.finding_id))
        compared |= {f.finding_id for f in group[:max_group]}
    seen: list[str] = []
    for keep, dups in out:
        assert dups
        assert dups == sorted(dups)
        seen += [keep, *dups]
        assert all(_score(by_id[keep]) >= _score(by_id[d]) for d in dups)
        assert len({(by_id[x].entity_type, by_id[x].entity_id) for x in [keep, *dups]}) == 1
    assert len(seen) == len(set(seen))
    assert set(seen) <= compared


# --- run_dedup (U06-103) ----------------------------------------------------------------------


class _FakeBoard:
    def __init__(self, findings: list[Finding]) -> None:
        self.findings = findings
        self.filters: list[FindingFilter] = []
        self.merges: list[tuple[str, list[str]]] = []

    async def list_findings(self, flt: FindingFilter) -> list[Finding]:
        self.filters.append(flt)
        return list(self.findings)

    async def merge(self, keep_id: str, dup_ids: list[str]) -> None:
        self.merges.append((keep_id, dup_ids))


def _env(board: _FakeBoard, depth: str = "standard") -> RunEnv:
    return cast("RunEnv", SimpleNamespace(run=run_row(depth), bb=board, cfg=PipelinesConfig()))


def _no_crosscheck(monkeypatch: pytest.MonkeyPatch, ids: set[str] | None = None) -> None:
    monkeypatch.setattr(dedup, "_crosscheck_task_ids", lambda run_id: set(ids or ()))


def test_it06_27_fast_mode_uses_query_sets(monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-27 fast depth: query_set mode without embeddings; components merged on the board."""
    _no_crosscheck(monkeypatch)

    def boom(text: str) -> np.ndarray:
        raise AssertionError(text)

    monkeypatch.setattr(dedup, "embed_query", boom)
    board = _FakeBoard([finding(1, confidence=0.9), finding(2)])
    with structlog.testing.capture_logs() as logs:
        merged = asyncio.run(dedup.run_dedup(_env(board, "fast")))
    assert merged == 1
    assert board.merges == [(board.findings[0].finding_id, [board.findings[1].finding_id])]
    assert board.filters[0].status == {"proposed"}
    done = [e for e in logs if e["event"] == "harness.dedup.completed"]
    assert done[0]["mode"] == "query_set"
    assert done[0]["merged"] == 1


def test_it06_27_embedding_mode_masks_markers(monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-27 embedding mode: claim text with markers replaced by <num>; vectors decide."""
    _no_crosscheck(monkeypatch)
    texts: list[str] = []

    def embed(text: str) -> np.ndarray:
        texts.append(text)
        return _unit(1, 0)

    monkeypatch.setattr(dedup, "embed_query", embed)
    board = _FakeBoard([finding(1, confidence=0.9), finding(2, claim="T1 saw [[n1]] [[n1]]")])
    with structlog.testing.capture_logs() as logs:
        merged = asyncio.run(dedup.run_dedup(_env(board)))
    assert merged == 1
    assert sorted(texts) == ["T1 saw <num> <num>", "Team t1 had <num> incidents"]
    assert [e["mode"] for e in logs if e["event"] == "harness.dedup.completed"] == ["embedding"]


def test_it06_27_crosscheck_findings_are_excluded(monkeypatch: pytest.MonkeyPatch) -> None:
    """IT06-27 findings authored by crosscheck tasks never join a dedup component."""
    _no_crosscheck(monkeypatch, {tid(7)})
    board = _FakeBoard([finding(1), finding(2, task=7)])
    assert asyncio.run(dedup.run_dedup(_env(board, "fast"))) == 0
    assert board.merges == []


@pytest.mark.parametrize("exc", [ModelUnavailable("down"), ConfigError("mismatch")])
def test_it06_27_embedding_failure_degrades(monkeypatch: pytest.MonkeyPatch, exc: Any) -> None:
    """IT06-27 embedding stub raising: query_set fallback, `harness.dedup.degraded` logged."""
    _no_crosscheck(monkeypatch)

    def raising(text: str) -> np.ndarray:
        raise exc

    monkeypatch.setattr(dedup, "embed_query", raising)
    board = _FakeBoard([finding(1, confidence=0.9), finding(2, numbers=[number(1, q=1)])])
    with structlog.testing.capture_logs() as logs:
        merged = asyncio.run(dedup.run_dedup(_env(board)))
    assert merged == 1
    degraded = [e for e in logs if e["event"] == "harness.dedup.degraded"]
    assert degraded
    assert degraded[0]["log_level"] == "warning"
    done = [e for e in logs if e["event"] == "harness.dedup.completed"]
    assert done[0]["mode"] == "query_set"
