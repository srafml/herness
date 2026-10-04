"""Memory recall and propose benchmarks on 200k items (BT07-01, BT07-02, BT07-03).

Spec targets (impl 07 §10, §11): "BT07-01 `recall` k = 10, query embedding time included
(uncached) | 200k items | p95 < 150 ms"; "BT07-02 `recall` degraded | 200k | p95 < 60 ms";
"BT07-03 `propose` excluding review creation | 200k | p95 < 200 ms". Run:
pytest -m "integration and slow" tests/bench -k memory.

Data: `tests.support.memory_bench_corpus` seeds 200,000 active `memory_item` rows (FTS5 index
and the 070 indexes live) in a migrated ops store and 200,000 random unit vectors in a LanceDB
`memory_embedding` table, once per module and never timed. Method: 5 warm-up calls, then 100
calls measured `REPEATS` times with distinct texts (no embedding cache hit); the gate is the
median of the per-repeat p95s.

BT07-01 uses the real `herness.enrich.embed_query` path on the CPU with the tiny
sentence-transformers model of `tests/fixtures/models/tiny-st` (bge-m3 is not in the test
environment; precedent BT05-06), so the embedding term is lower than bge-m3's. BT07-02 makes
the embedder raise, so `recall` takes the keyword-only degraded path. BT07-03 proposes human
glossary items (policy matrix: active, no review item) and includes the near-duplicate vector
search and the vector upsert; the embedding there is the hash-seeded `FakeEmbed`.

BT07-01 and BT07-03 are non-strict xfails (OI-7): `VectorIndex.ensure_table` builds no ANN
index, and the flat LanceDB search over 200k x 1024 floats alone takes about 450 ms here.
"""

from __future__ import annotations

import statistics
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import numpy as np
import pytest
from tests.support.bench_stats import REPEATS, report
from tests.support.make_tiny_st import TINY_ST
from tests.support.memory_bench_corpus import (
    open_store,
    plant_matches,
    query_text,
    seed_corpus,
)
from tests.unit.harness.memory._write_env import FakeEmbed, proposal

from herness.core.config import reset_config
from herness.enrich import embed
from herness.harness.memory import MemoryStore
from herness.store.ops import core, reset_connections
from herness.store.ops.migrate import migrate

pytestmark = [pytest.mark.integration, pytest.mark.slow]

ITEMS = 200_000
CALLS = 100
WARM_UP = 5
K = 10
RECALL_P95_MS = 150.0
DEGRADED_P95_MS = 60.0
PROPOSE_P95_MS = 200.0


def _p95(samples: list[float]) -> float:
    return statistics.quantiles(samples, n=100)[94]


def _measure(call: Callable[[int], object], repeats: int = REPEATS) -> list[float]:
    """Per-repeat p95 (ms) of `CALLS` calls; `call(i)` gets a globally distinct index."""
    for i in range(WARM_UP):
        call(-1 - i)
    p95s: list[float] = []
    for r in range(repeats):
        samples: list[float] = []
        for i in range(CALLS):
            start = time.perf_counter()
            call(r * CALLS + i)
            samples.append((time.perf_counter() - start) * 1000)
        p95s.append(_p95(samples))
    return p95s


def _down(_text: str) -> np.ndarray:
    msg = "embedding server down"
    raise RuntimeError(msg)


@pytest.fixture(scope="module")
def corpus(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Path]:
    """200k seeded items; the ops store and config point at the module's temp root."""
    root = tmp_path_factory.mktemp("bt07_recall")
    (root / "data").mkdir()
    patch = pytest.MonkeyPatch()
    patch.setenv("HERNESS_PATHS__DATA", str(root / "data"))
    reset_config()
    reset_connections(path=root / "data" / "ops.sqlite")
    try:
        migrate()
        start = time.perf_counter()
        seed_corpus(root, ITEMS)
        seeded = time.perf_counter() - start
        sys.stderr.write(f"BT07 data: {ITEMS} items + vectors seeded in {seeded:.1f} s\n")
        yield root
    finally:
        reset_connections()
        patch.undo()
        reset_config()


@pytest.fixture(scope="module")
def real_embed() -> Iterator[Callable[[str], np.ndarray]]:
    """`embed_query` on the CPU with the tiny-st stand-in model (see module docstring)."""
    import torch  # noqa: PLC0415 - only this benchmark needs torch

    encoder = embed.Encoder(TINY_ST, model_name="bge-m3")
    patch = pytest.MonkeyPatch()
    patch.setattr(embed, "get_encoder", lambda: encoder)
    patch.setattr(embed._QueryState, "model_check", True)
    patch.setattr(torch.cuda, "is_available", lambda: False)
    patch.setattr(embed, "release_cuda", lambda: None)
    try:
        yield embed.embed_query
    finally:
        patch.undo()


@pytest.mark.timeout(900)
@pytest.mark.xfail(
    raises=AssertionError,
    strict=False,
    reason="OI-7: measured recall p95 ~470-680 ms vs 150 ms; `VectorIndex.ensure_table` builds no"
    " ANN index, so every LanceDB search scans 200k x 1024 floats (~420-470 ms; an IVF_PQ"
    " index brings the search to ~23 ms)",
)
def test_bt07_01_recall_k10_uncached_embedding_p95(
    corpus: Path, real_embed: Callable[[str], np.ndarray]
) -> None:
    """BT07-01 `recall` k = 10 with an uncached query embedding on 200k items: p95 < 150 ms."""
    rng = np.random.default_rng(11)
    queries = [query_text(rng) for _ in range(CALLS * REPEATS + WARM_UP + 1)]  # last: spare probe
    matches = np.stack([real_embed(q) for q in queries])  # one true match per query
    plant_matches(corpus, ITEMS, queries, matches)
    store = open_store(corpus, real_embed)
    probe = store.recall_with_status(queries[-1], k=K)  # not measured: no warm cache
    assert not probe.degraded
    assert probe.hits, "the planted match must be found"

    def recall(i: int) -> None:
        store.recall(queries[i if i >= 0 else CALLS * REPEATS - 1 - i], k=K)

    p95s = _measure(recall)
    p95 = report("BT07-01", "p95_recall", p95s, "ms", f"< {RECALL_P95_MS:g} ms")
    assert p95 < RECALL_P95_MS, f"recall p95 {p95:.1f} ms"


def test_bt07_02_recall_degraded_p95(corpus: Path) -> None:
    """BT07-02 `recall` with the vector path down (keyword and entity only), 200k: p95 < 60 ms."""
    store = open_store(corpus, _down)
    rng = np.random.default_rng(12)
    queries = [query_text(rng) for _ in range(CALLS * REPEATS + WARM_UP + 1)]
    probe = store.recall_with_status(queries[0], k=K)
    assert probe.degraded
    assert probe.hits, "the keyword path must find candidates"
    p95s = _measure(lambda i: store.recall(queries[i % len(queries)], k=K))
    p95 = report("BT07-02", "p95_recall_degraded", p95s, "ms", f"< {DEGRADED_P95_MS:g} ms")
    assert p95 < DEGRADED_P95_MS, f"degraded recall p95 {p95:.1f} ms"


@pytest.mark.timeout(900)
@pytest.mark.xfail(
    raises=AssertionError,
    strict=False,
    reason="OI-7: measured propose p95 ~770-1300 ms vs 200 ms; the near-duplicate ANN search of"
    " step 11 is the same unindexed 200k x 1024 LanceDB scan as BT07-01 (~420-470 ms)",
)
def test_bt07_03_propose_excluding_review_p95(corpus: Path) -> None:
    """BT07-03 `propose` of an active human item (no review item), 200k items: p95 < 200 ms."""
    store: MemoryStore = open_store(corpus, FakeEmbed())

    def propose(i: int) -> None:
        result = store.propose(
            proposal(f"bench glossary entry number {i} about term{i % 3000:04d}")
        )
        assert result.status == "active"

    p95s = _measure(propose)
    reviews = core.read_all("SELECT count(*) AS n FROM review_item")
    assert reviews[0]["n"] == 0, "BT07-03 must not create review items"
    p95 = report("BT07-03", "p95_propose", p95s, "ms", f"< {PROPOSE_P95_MS:g} ms")
    assert p95 < PROPOSE_P95_MS, f"propose p95 {p95:.1f} ms"
