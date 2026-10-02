"""Tests for herness.enrich.purge_record (U03-145; T03-34): UT03-133, UT03-134."""

from __future__ import annotations

import errno
from pathlib import Path

import pytest
from structlog.testing import capture_logs
from tests.unit.enrich._purge_support import (
    CHG1,
    H1,
    H2,
    H3,
    HP,
    INC1,
    INC2,
    INC3,
    PurgeEnv,
    purge_env,
)

from herness.core.errors import StoreBusy
from herness.enrich import purge
from herness.enrich.purge import purge_record

pytestmark = pytest.mark.unit

ZEROS = {
    "embeddings_deleted": 0,
    "cache_rows_deleted": 0,
    "label_rows_deleted": 0,
    "pair_rows_deleted": 0,
    "hashes_shared": 0,
}
PAIR = f"{INC1}|{CHG1}"


@pytest.fixture
def env(tmp_path: Path) -> PurgeEnv:
    return purge_env(tmp_path)


# UT03-133 -----------------------------------------------------------------------------------


def test_ut03_133_shared_hash_keeps_cache_and_other_vector(env: PurgeEnv) -> None:
    """UT03-133 a hash shared with another live record: vector removed, cache kept, shared 1."""
    env.add_vectors([(INC1, H1), (INC2, H1), (INC3, H3)])
    env.warehouse([(INC1, H1), (INC2, H1), (INC3, H3)])
    env.cache([H1, H3])

    counts = purge_record(INC1)

    assert counts == {**ZEROS, "embeddings_deleted": 1, "hashes_shared": 1}
    assert env.vector_ids() == [(INC2, H1), (INC3, H3)]
    assert env.cache_hashes() == [H1, H3]


def test_ut03_133_hash_shared_only_through_the_warehouse(env: PurgeEnv) -> None:
    """UT03-133 a hash present only in `CURRENT` `text_redacted` for another record is shared."""
    env.add_vectors([(INC1, H1)])
    env.warehouse([(INC1, H1), (INC2, H1)])
    env.cache([H1])

    counts = purge_record(INC1)

    assert (counts["embeddings_deleted"], counts["hashes_shared"]) == (1, 1)
    assert env.cache_hashes() == [H1]


def test_ut03_133_shared_hash_keeps_other_label_rows_but_drops_the_records(env: PurgeEnv) -> None:
    """UT03-133 label rows of the purged record go; a shared hash's other rows stay."""
    env.add_vectors([(INC1, H1), (INC2, H1)])
    env.labels("human", [(INC1, H1, "q_a"), (INC2, H1, "q_a")])

    counts = purge_record(INC1)

    assert (counts["label_rows_deleted"], counts["hashes_shared"]) == (1, 1)
    assert env.label_rows("human") == [(INC2, H1)]


# UT03-134 -----------------------------------------------------------------------------------


def _full_record(env: PurgeEnv) -> None:
    env.add_vectors([(INC1, H1), (INC2, H2)])
    env.warehouse([(INC1, H1), (INC2, H2)])
    env.cache([H1, H2, HP])
    env.labels("teacher", [(INC1, H1, "q_a"), (INC2, H2, "q_a")])
    env.labels("human", [(INC1, H1, "q_a"), (PAIR, HP, "change_caused_pair")])
    env.labels("gold", [(INC1, H1, "q_gold"), (INC2, H2, "q_a")])
    env.labels("gold_reviews", [(INC1, H1, "q_gold")])
    env.pairs([(INC1, CHG1, HP), (INC2, CHG1, H3)])


def test_ut03_134_unshared_hash_pair_and_labels_removed_then_zeros(env: PurgeEnv) -> None:
    """UT03-134 unshared hash, pair index row, label rows: all removed; second call zeros."""
    _full_record(env)

    with capture_logs() as logs:
        first = purge_record(INC1)

    assert first == {
        "embeddings_deleted": 1,
        "cache_rows_deleted": 2,  # H1 and the pair hash HP
        "label_rows_deleted": 5,  # teacher, human, pair label, gold, gold review
        "pair_rows_deleted": 1,
        "hashes_shared": 0,
    }
    assert env.vector_ids() == [(INC2, H2)]
    assert env.cache_hashes() == [H2]
    assert env.label_rows("teacher") == [(INC2, H2)]
    assert env.label_rows("human") == []
    assert env.label_rows("gold") == [(INC2, H2)]
    assert env.label_rows("gold_reviews") == []
    assert env.pair_rows() == [(INC2, CHG1, H3)]
    gold = [e for e in logs if e["event"] == "enrich.purge.gold_modified"]
    assert [(e["log_level"], e["question"]) for e in gold] == [("warning", "q_gold")]
    done = [e for e in logs if e["event"] == "enrich.purge.completed"]
    assert len(done) == 1
    assert done[0]["log_level"] == "info"
    assert {k: done[0][k] for k in first} == first
    assert "INC1" not in repr(logs)

    with capture_logs() as again:
        assert purge_record(INC1) == ZEROS
    assert not [e for e in again if e["event"] == "enrich.purge.gold_modified"]


def test_ut03_134_change_side_of_a_pair_is_purged(env: PurgeEnv) -> None:
    """UT03-134 purging the change removes pair rows and pair-hash rows naming it."""
    env.cache([HP, H2])
    env.labels("human", [(PAIR, HP, "change_caused_pair")])
    part = env.pairs([(INC1, CHG1, HP)])

    counts = purge_record(CHG1)

    assert (counts["pair_rows_deleted"], counts["cache_rows_deleted"]) == (1, 1)
    assert counts["label_rows_deleted"] == 1
    assert not part.exists()  # an emptied index part is deleted
    assert env.cache_hashes() == [H2]


def test_ut03_134_no_current_warehouse_and_no_stores(env: PurgeEnv) -> None:
    """UT03-134 without `CURRENT`, cache, labels or pair index the purge still runs: zeros."""
    assert purge_record(INC1) == ZEROS
    env.add_vectors([(INC1, H1)])
    env.cache([H1])
    assert purge_record(INC1) == {**ZEROS, "embeddings_deleted": 1, "cache_rows_deleted": 1}


def test_ut03_134_stale_tmp_files_are_removed(env: PurgeEnv) -> None:
    """UT03-134 leftover `.part-*.parquet.tmp` files under labels and pairs are deleted."""
    env.labels("human", [(INC2, H2, "q_a")])
    env.pairs([(INC2, CHG1, H3)])
    label_dir = env.paths.labels_dir("qs-2026-10-01.1", "human")
    stale = [label_dir / ".part-x.parquet.tmp", env.paths.pairs_dir() / ".part-y.parquet.tmp"]
    for path in stale:
        path.write_bytes(b"partial")

    purge_record(INC1)

    assert not any(path.exists() for path in stale)
    assert env.label_rows("human") == [(INC2, H2)]


def test_ut03_134_label_rows_with_malformed_hash_go_by_record_id(env: PurgeEnv) -> None:
    """UT03-134 a label row of the record whose hash is not 32-hex is removed by record_id."""
    env.labels("human", [(INC1, "not-a-hash", "q_a"), (INC2, H2, "q_a")])
    counts = purge_record(INC1)
    assert counts["label_rows_deleted"] == 1
    assert env.label_rows("human") == [(INC2, H2)]


def test_ut03_134_io_error_is_store_busy(env: PurgeEnv, monkeypatch: pytest.MonkeyPatch) -> None:
    """UT03-134 an OS error reading a label or pair part -> StoreBusy, no path in message."""
    env.pairs([(INC1, CHG1, HP)])

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError(errno.EIO, "disk", str(env.root))

    monkeypatch.setattr(purge.pq, "read_table", boom)
    with pytest.raises(StoreBusy) as info:
        purge_record(INC1)
    assert str(env.root) not in str(info.value)
