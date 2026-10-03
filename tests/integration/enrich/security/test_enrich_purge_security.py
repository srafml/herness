"""Security test of the privacy purge (impl 03 T03-34): ST03-14 (TH03-12).

A record present in vectors, the `CURRENT` warehouse, the decision cache (two versions), every
label kind and the pair index is purged through the public facade; every store is then read
back through freshly opened handles (a new LanceDB connection, new Parquet readers).
"""

from __future__ import annotations

from pathlib import Path

import pyarrow.parquet as pq
import pytest
from tests.unit.enrich._purge_support import (
    CHG1,
    H1,
    H2,
    H3,
    HP,
    HX,
    INC1,
    INC2,
    OJ_V,
    PurgeEnv,
    purge_env,
)

import herness.enrich
from herness.enrich.cache import CACHE_SCHEMA
from herness.store.vectors import VectorStore

pytestmark = pytest.mark.integration

PAIR = f"{INC1}|{CHG1}"
OLD_QSV = "qs-2026-09-01.1"


def _every_hash_on_disk(env: PurgeEnv) -> set[str]:
    """`content_hash` of every Parquet file under the cache and labels trees (fresh reads)."""
    found: set[str] = set()
    for root in (env.paths.data_root / "cache", env.paths.data_root / "labels"):
        for part in root.rglob("*.parquet"):
            table = pq.read_table(part, columns=["content_hash"], partitioning=None)
            found.update(table.column(0).to_pylist())
    return found


def _every_record_on_disk(env: PurgeEnv) -> set[str]:
    found: set[str] = set()
    for part in (env.paths.data_root / "labels").rglob("*.parquet"):
        found.update(pq.read_table(part, columns=["record_id"], partitioning=None)[0].to_pylist())
    for part in env.paths.pairs_dir().rglob("*.parquet"):
        table = pq.read_table(part, partitioning=None)
        found.update(table["incident_id"].to_pylist() + table["change_id"].to_pylist())
    return found


def test_st03_14_purged_record_remains_nowhere_but_shared_hash_rows(tmp_path: Path) -> None:
    """ST03-14 vectors, cache, labels, pair index: none remain (except shared hash rows)."""
    env = purge_env(tmp_path)
    env.add_vectors([(INC1, H1), (INC1, HX), (INC2, H2), (INC2, HX)])
    env.warehouse([(INC1, H1), (INC2, H2)])
    env.cache([H1, H2, HP, HX])
    old = env.paths.cache_partition(OLD_QSV, "openjev", OJ_V)
    old.mkdir(parents=True)
    pq.write_table(
        pq.read_table(env.paths.cache_partition("qs-2026-10-01.1", "openjev", OJ_V))
        .select(CACHE_SCHEMA.names)
        .cast(CACHE_SCHEMA),
        old / "part-old.parquet",
    )
    env.labels("teacher", [(INC1, H1, "q_a"), (INC2, H2, "q_a")])
    env.labels("human", [(INC1, H1, "q_a"), (PAIR, HP, "change_caused_pair"), (INC2, HX, "q_a")])
    env.labels("gold", [(INC1, H1, "q_a")])
    env.labels("gold_reviews", [(INC1, H1, "q_a")])
    env.pairs([(INC1, CHG1, HP), (INC2, CHG1, H3)])
    env.pairs([(INC1, CHG1, HP)], build="20260901-120000-ABCDEF")

    counts = herness.enrich.purge_record(INC1)

    assert counts["embeddings_deleted"] == 2
    assert counts["hashes_shared"] == 1  # HX: INC2 still has a vector with it
    fresh = VectorStore(env.paths.vectors_dir()).table("ticket_embedding").to_arrow()
    assert INC1 not in fresh["record_id"].to_pylist()
    assert sorted(fresh["content_hash"].to_pylist()) == sorted([H2, HX])
    assert INC1 not in env.ids_in_every_version()  # no older table version keeps it
    on_disk = _every_hash_on_disk(env)
    assert {H1, HP} & on_disk == set()
    assert {H2, HX} <= on_disk  # the other record's rows and the shared hash stay
    assert {INC1, PAIR} & _every_record_on_disk(env) == set()
    assert env.pair_rows() == [(INC2, CHG1, H3)]

    assert herness.enrich.purge_record(INC1) == dict.fromkeys(counts, 0)
